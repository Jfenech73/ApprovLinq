import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import analytics, auth, admin, batches, health, tenant

logger = logging.getLogger(__name__)

try:
    models.Base.metadata.create_all(bind=engine)
except Exception as exc:
    logger.warning("create_all failed (non-fatal): %s", exc)


def ensure_runtime_schema() -> None:
    """Apply incremental schema migrations at startup.

    Each statement runs in its OWN connection and transaction so that one
    failure (e.g. column already exists) never aborts the remaining
    statements.  All statements are idempotent (IF NOT EXISTS / IF EXISTS).
    """
    if engine.dialect.name != "postgresql":
        return

    statements: list[str] = [
        # ── tenants ──────────────────────────────────────────────────────────
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) NOT NULL DEFAULT 'summary'",

        # ── tenant_suppliers ─────────────────────────────────────────────────
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS supplier_account_code VARCHAR(100)",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS default_nominal VARCHAR(100)",
        # Back-fill supplier_account_code from posting_account where blank
        "UPDATE tenant_suppliers SET supplier_account_code = COALESCE(NULLIF(supplier_account_code, ''), posting_account) WHERE supplier_account_code IS NULL OR supplier_account_code = ''",
        # Back-fill company_id from the first company in the same tenant
        "UPDATE tenant_suppliers AS ts SET company_id = c.id FROM companies AS c WHERE ts.company_id IS NULL AND c.tenant_id = ts.tenant_id",
        "CREATE INDEX IF NOT EXISTS ix_tenant_suppliers_tenant_company_account_code ON tenant_suppliers (tenant_id, company_id, supplier_account_code)",

        # ── tenant_nominal_accounts ──────────────────────────────────────────
        "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS company_id UUID",
        "UPDATE tenant_nominal_accounts AS na SET company_id = c.id FROM companies AS c WHERE na.company_id IS NULL AND c.tenant_id = na.tenant_id",
        "CREATE INDEX IF NOT EXISTS ix_tenant_nominals_tenant_company_account_code ON tenant_nominal_accounts (tenant_id, company_id, account_code)",
        "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE",

        # ── invoice_batches ──────────────────────────────────────────────────
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) DEFAULT 'summary'",
        "UPDATE invoice_batches SET scan_mode = COALESCE(NULLIF(scan_mode, ''), 'summary')",

        # ── invoice_files ────────────────────────────────────────────────────
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER",
        "UPDATE invoice_files AS f SET company_id = b.company_id FROM invoice_batches AS b WHERE f.company_id IS NULL AND b.id = f.batch_id",

        # ── invoice_rows ─────────────────────────────────────────────────────
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS supplier_posting_account VARCHAR(100)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS nominal_account_code VARCHAR(100)",
        "ALTER TABLE invoice_rows ALTER COLUMN method_used TYPE VARCHAR(200)",
        "UPDATE invoice_rows AS r SET company_id = b.company_id FROM invoice_batches AS b WHERE r.company_id IS NULL AND b.id = r.batch_id",

        # ── tenant_suppliers — new columns ───────────────────────────────────
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS vat_number VARCHAR(100)",

        # ── invoice_rows — new columns ────────────────────────────────────────
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_reasons VARCHAR(500)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS page_quality_score NUMERIC(4,2)",

        # ── supplier_patterns (new table) ────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS supplier_patterns ("
            "id SERIAL PRIMARY KEY,"
            "tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
            "company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,"
            "supplier_id INTEGER NOT NULL REFERENCES tenant_suppliers(id) ON DELETE CASCADE,"
            "keywords TEXT,"
            "hit_count INTEGER NOT NULL DEFAULT 1,"
            "last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "CONSTRAINT uq_supplier_pattern UNIQUE (tenant_id, company_id, supplier_id)"
            ")"
        ),
    ]

    ok = skipped = 0
    for stmt in statements:
        # Each statement gets its own connection + transaction so that one
        # failure never puts subsequent statements into an aborted transaction.
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            ok += 1
        except Exception as stmt_exc:
            skipped += 1
            logger.debug("Schema migration skipped (%s): %.120s", type(stmt_exc).__name__, stmt)

    logger.info("ensure_runtime_schema: %d applied, %d already-present/skipped", ok, skipped)


try:
    ensure_runtime_schema()
except Exception as exc:
    logger.warning("ensure_runtime_schema failed (non-fatal): %s", exc)

app = FastAPI(title=settings.app_name)
base_dir = Path(__file__).resolve().parent
static_dir = base_dir / "static"
_version_file = base_dir.parent / "VERSION"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
async def recover_stuck_batches() -> None:
    from app.db.session import SessionLocal
    from app.db.models import InvoiceBatch
    db = SessionLocal()
    try:
        stuck = db.query(InvoiceBatch).filter(InvoiceBatch.status == "processing").all()
        for batch in stuck:
            batch.status = "partial"
            batch.notes = "Processing was interrupted by a server restart. Re-process to complete."
            batch.processed_at = datetime.now(timezone.utc)
        if stuck:
            db.commit()
            logger.info("Recovered %d stuck batch(es) from 'processing' status on startup", len(stuck))
    except Exception as exc:
        logger.warning("Failed to recover stuck batches on startup: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@app.get("/version")
def get_version():
    version = _version_file.read_text().strip() if _version_file.exists() else "0.0.0"
    return JSONResponse({"version": version})


@app.get("/")
def frontend():
    path = static_dir / "login.html"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=500, detail=f"Frontend file not found: {path}")


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(batches.router)
app.include_router(admin.router)
app.include_router(tenant.router)
app.include_router(analytics.router)

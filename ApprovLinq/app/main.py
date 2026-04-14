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
from app.routers import analytics, auth, admin, admin_export_templates, batches, health, tenant

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
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_priority VARCHAR(20)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS review_fields VARCHAR(300)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS page_quality_score NUMERIC(4,2)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS classification_method VARCHAR(50)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS supplier_match_method VARCHAR(50)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS totals_reconciliation_status VARCHAR(50)",

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

        # >>> REVIEW_PACK startup_alters
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS approved_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS exported_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS exported_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS reopened_by UUID REFERENCES users(id)",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS current_export_version INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS invoice_row_corrections (
            row_id BIGINT PRIMARY KEY REFERENCES invoice_rows(id) ON DELETE CASCADE,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            supplier_name TEXT, supplier_posting_account VARCHAR(100),
            nominal_account_code VARCHAR(100), invoice_number TEXT,
            invoice_date DATE, description TEXT,
            net_amount NUMERIC(14,2), vat_amount NUMERIC(14,2), total_amount NUMERIC(14,2),
            currency VARCHAR(20), tax_code VARCHAR(50),
            reviewed_fields TEXT, row_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by UUID REFERENCES users(id))""",
        "CREATE INDEX IF NOT EXISTS ix_corrections_batch ON invoice_row_corrections(batch_id)",
        """CREATE TABLE IF NOT EXISTS invoice_row_field_audits (
            id BIGSERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            row_id BIGINT NOT NULL,
            field_name VARCHAR(80) NOT NULL,
            old_value TEXT, new_value TEXT,
            action VARCHAR(40) NOT NULL, note TEXT,
            rule_created BOOLEAN NOT NULL DEFAULT FALSE,
            force_added BOOLEAN NOT NULL DEFAULT FALSE,
            user_id UUID REFERENCES users(id), username VARCHAR(255),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_audits_batch_row ON invoice_row_field_audits(batch_id, row_id)",
        """CREATE TABLE IF NOT EXISTS correction_rules (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
            rule_type VARCHAR(40) NOT NULL,
            field_name VARCHAR(80) NOT NULL,
            source_pattern TEXT NOT NULL,
            target_value TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            disabled_by UUID REFERENCES users(id),
            disabled_at TIMESTAMPTZ,
            origin_batch_id UUID,
            origin_row_id BIGINT)""",
        "CREATE INDEX IF NOT EXISTS ix_rules_lookup ON correction_rules(tenant_id, rule_type, field_name, source_pattern, active)",
        """CREATE TABLE IF NOT EXISTS remap_hints (
            id BIGSERIAL PRIMARY KEY,
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            company_id UUID REFERENCES companies(id) ON DELETE CASCADE,
            supplier_id BIGINT REFERENCES tenant_suppliers(id) ON DELETE CASCADE,
            supplier_name_snapshot TEXT,
            field_name VARCHAR(80) NOT NULL,
            page_no INTEGER,
            x NUMERIC(8,4), y NUMERIC(8,4), w NUMERIC(8,4), h NUMERIC(8,4),
            source_batch_id UUID, source_file_id BIGINT, source_row_id BIGINT,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        "CREATE INDEX IF NOT EXISTS ix_remap_lookup ON remap_hints(supplier_id, field_name, active)",
        """CREATE TABLE IF NOT EXISTS batch_export_events (
            id BIGSERIAL PRIMARY KEY,
            batch_id UUID NOT NULL REFERENCES invoice_batches(id) ON DELETE CASCADE,
            export_version INTEGER NOT NULL,
            template_id BIGINT,
            exported_by UUID REFERENCES users(id),
            exported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            file_path TEXT, row_count INTEGER)""",
        "CREATE INDEX IF NOT EXISTS ix_export_events_batch ON batch_export_events(batch_id)",
        # <<< REVIEW_PACK startup_alters


        # ── export_templates ──────────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS export_templates ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            "name VARCHAR(255) NOT NULL,"
            "description TEXT,"
            "accounting_system VARCHAR(100),"
            "version_label VARCHAR(50) NOT NULL DEFAULT 'v1',"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "is_system_default BOOLEAN NOT NULL DEFAULT FALSE,"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "created_by UUID REFERENCES users(id) ON DELETE SET NULL,"
            "updated_by UUID REFERENCES users(id) ON DELETE SET NULL"
            ")"
        ),

        # ── export_template_columns ───────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS export_template_columns ("
            "id SERIAL PRIMARY KEY,"
            "template_id UUID NOT NULL REFERENCES export_templates(id) ON DELETE CASCADE,"
            "column_order INTEGER NOT NULL DEFAULT 0,"
            "column_heading VARCHAR(255) NOT NULL,"
            "column_type VARCHAR(50) NOT NULL,"
            "source_field VARCHAR(100),"
            "static_value VARCHAR(500),"
            "transform_rule VARCHAR(200),"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "notes TEXT"
            ")"
        ),

        # ── export_template_columns — new columns ─────────────────────────────
        "ALTER TABLE export_template_columns ADD COLUMN IF NOT EXISTS condition_rules JSONB",

        # ── template_assignments ──────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS template_assignments ("
            "id SERIAL PRIMARY KEY,"
            "template_id UUID NOT NULL REFERENCES export_templates(id) ON DELETE CASCADE,"
            "tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
            "company_id UUID REFERENCES companies(id) ON DELETE CASCADE,"
            "is_active BOOLEAN NOT NULL DEFAULT TRUE,"
            "assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            "assigned_by UUID REFERENCES users(id) ON DELETE SET NULL"
            ")"
        ),

        # ── admin_audit_logs ──────────────────────────────────────────────────
        (
            "CREATE TABLE IF NOT EXISTS admin_audit_logs ("
            "id SERIAL PRIMARY KEY,"
            "event_type VARCHAR(100) NOT NULL,"
            "entity_type VARCHAR(100) NOT NULL,"
            "entity_id VARCHAR(255),"
            "user_id UUID REFERENCES users(id) ON DELETE SET NULL,"
            "notes TEXT,"
            "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
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


@app.on_event("startup")
async def run_file_retention_cleanup() -> None:
    """Delete uploaded PDFs and exported XLSXs that are older than the
    configured retention window (default 5 days).  Runs once at startup so
    the server self-cleans on every deploy/restart without needing a cron job.

    Safety rules:
    - Only files strictly older than file_retention_days are removed.
    - Whole batch-upload folders are removed only when ALL files inside them
      are past the retention window (avoids breaking active review sessions).
    - Export files are removed individually by mtime.
    - DB records are not touched — the rows remain for analytics/audit history.
    """
    import time
    import shutil
    from pathlib import Path as _Path

    retention_seconds = settings.file_retention_days * 86_400
    now = time.time()
    removed_files = removed_folders = 0

    # ── Uploaded batch folders ────────────────────────────────────────────────
    try:
        upload_root = _Path(settings.upload_dir).resolve()
        if upload_root.exists():
            for batch_folder in upload_root.iterdir():
                if not batch_folder.is_dir():
                    continue
                files_in_folder = list(batch_folder.rglob("*"))
                pdf_files = [f for f in files_in_folder if f.is_file()]
                if not pdf_files:
                    # Empty folder — remove it
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception:
                        pass
                    continue
                oldest_mtime = max(f.stat().st_mtime for f in pdf_files)
                if (now - oldest_mtime) > retention_seconds:
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception as exc:
                        logger.warning("Retention: could not remove folder %s: %s", batch_folder, exc)
    except Exception as exc:
        logger.warning("Retention: upload cleanup failed: %s", exc)

    # ── Exported XLSX files ───────────────────────────────────────────────────
    try:
        export_root = _Path(settings.export_dir).resolve()
        if export_root.exists():
            for export_file in export_root.rglob("*.xlsx"):
                if not export_file.is_file():
                    continue
                try:
                    age = now - export_file.stat().st_mtime
                    if age > retention_seconds:
                        export_file.unlink(missing_ok=True)
                        removed_files += 1
                except Exception as exc:
                    logger.warning("Retention: could not remove export %s: %s", export_file, exc)
    except Exception as exc:
        logger.warning("Retention: export cleanup failed: %s", exc)

    if removed_folders or removed_files:
        logger.info(
            "Retention cleanup: removed %d batch folder(s), %d export file(s) "
            "(retention=%d days)",
            removed_folders, removed_files, settings.file_retention_days,
        )


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
# >>> REVIEW_PACK router_register
from app.routers import review as _review_router
app.include_router(_review_router.router)
# <<< REVIEW_PACK router_register
app.include_router(admin.router)
app.include_router(admin_export_templates.router)
app.include_router(tenant.router)
app.include_router(analytics.router)

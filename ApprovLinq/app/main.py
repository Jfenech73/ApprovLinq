import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import auth, admin, batches, health, tenant

logger = logging.getLogger(__name__)

try:
    models.Base.metadata.create_all(bind=engine)
except Exception as exc:
    logger.warning("create_all failed (non-fatal): %s", exc)


def ensure_runtime_schema() -> None:
    statements: list[str] = []
    dialect = engine.dialect.name

    if dialect == "postgresql":
        statements = [
            "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) NOT NULL DEFAULT 'summary'",
            "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS supplier_account_code VARCHAR(100)",
            "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS default_nominal VARCHAR(100)",
            "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS company_id UUID",
            "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS company_id UUID",
            "UPDATE tenant_suppliers SET supplier_account_code = COALESCE(NULLIF(supplier_account_code, ''), posting_account) WHERE supplier_account_code IS NULL OR supplier_account_code = ''",
            "UPDATE tenant_suppliers ts SET company_id = c.id FROM companies c WHERE ts.company_id IS NULL AND c.tenant_id = ts.tenant_id",
            "UPDATE tenant_nominal_accounts na SET company_id = c.id FROM companies c WHERE na.company_id IS NULL AND c.tenant_id = na.tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_tenant_suppliers_tenant_company_account_code ON tenant_suppliers (tenant_id, company_id, supplier_account_code)",
            "CREATE INDEX IF NOT EXISTS ix_tenant_nominals_tenant_company_account_code ON tenant_nominal_accounts (tenant_id, company_id, account_code)",
            "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS company_id UUID",
            "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS company_id UUID",
            "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS company_id UUID",
            "UPDATE invoice_files f SET company_id = b.company_id FROM invoice_batches b WHERE f.company_id IS NULL AND b.id = f.batch_id",
            "UPDATE invoice_rows r SET company_id = b.company_id FROM invoice_batches b WHERE r.company_id IS NULL AND b.id = r.batch_id",
            "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) DEFAULT 'summary'",
            "UPDATE invoice_batches SET scan_mode = COALESCE(NULLIF(scan_mode, ''), 'summary')",
            "ALTER TABLE invoice_rows ALTER COLUMN method_used TYPE VARCHAR(200)",
        ]

    if not statements:
        return

    try:
        with engine.begin() as conn:
            for statement in statements:
                try:
                    conn.execute(text(statement))
                except Exception as stmt_exc:
                    logger.debug("Schema migration statement skipped: %s", stmt_exc)
    except Exception as conn_exc:
        logger.warning("ensure_runtime_schema connection failed (non-fatal): %s", conn_exc)


try:
    ensure_runtime_schema()
except Exception as exc:
    logger.warning("ensure_runtime_schema failed (non-fatal): %s", exc)

app = FastAPI(title=settings.app_name)
base_dir = Path(__file__).resolve().parent
static_dir = base_dir / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


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
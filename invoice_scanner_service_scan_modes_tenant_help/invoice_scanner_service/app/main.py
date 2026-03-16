from pathlib import Path

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import auth, admin, batches, health, tenant
from app.utils.logging_utils import configure_logging

configure_logging()
models.Base.metadata.create_all(bind=engine)


def ensure_runtime_schema() -> None:
    statements: list[str] = []
    dialect = engine.dialect.name

    if dialect == "postgresql":
        statements = [
            "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS supplier_account_code VARCHAR(100)",
            "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS default_nominal VARCHAR(100)",
            "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS customer_code VARCHAR(100)",
            "UPDATE tenant_suppliers SET supplier_account_code = COALESCE(NULLIF(supplier_account_code, ''), posting_account) WHERE supplier_account_code IS NULL OR supplier_account_code = ''",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tenant_suppliers_tenant_account_code ON tenant_suppliers (tenant_id, supplier_account_code) WHERE supplier_account_code IS NOT NULL",
        ]
    elif dialect == "sqlite":
        statements = [
            "ALTER TABLE tenant_suppliers ADD COLUMN supplier_account_code VARCHAR(100)",
            "ALTER TABLE tenant_suppliers ADD COLUMN default_nominal VARCHAR(100)",
            "ALTER TABLE invoice_rows ADD COLUMN customer_code VARCHAR(100)",
            "UPDATE tenant_suppliers SET supplier_account_code = COALESCE(NULLIF(supplier_account_code, ''), posting_account) WHERE supplier_account_code IS NULL OR supplier_account_code = ''",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_tenant_suppliers_tenant_account_code ON tenant_suppliers (tenant_id, supplier_account_code)",
        ]

    if not statements:
        return

    with engine.begin() as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
            except Exception:
                pass


ensure_runtime_schema()

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

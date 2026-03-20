from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the ApprovLinq project root is on sys.path regardless of the
# working directory from which this script is invoked.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Validate database connectivity before doing anything else ─────────────────
try:
    from app.config import settings
    _db_url = (settings.database_url or "").strip()
    if not _db_url:
        print("ERROR: DATABASE_URL is not set.")
        print("  Set it in ApprovLinq/.env or as an environment variable.")
        print("  Example: DATABASE_URL=postgresql://user:password@host:5432/dbname")
        sys.exit(1)
except Exception as _cfg_err:
    print(f"ERROR: Could not load configuration: {_cfg_err}")
    print("  Make sure ApprovLinq/.env exists and DATABASE_URL is defined.")
    sys.exit(1)

# ── Ensure all database tables exist (safe on an already-seeded DB) ───────────
try:
    from app.db import models
    from app.db.session import engine
    from sqlalchemy import text

    models.Base.metadata.create_all(bind=engine)

    # Apply any extra runtime migrations (new columns, indexes, etc.)
    _RUNTIME_STMTS = [
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) NOT NULL DEFAULT 'summary'",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS supplier_account_code VARCHAR(100)",
        "ALTER TABLE tenant_suppliers ADD COLUMN IF NOT EXISTS default_nominal VARCHAR(100)",
        "ALTER TABLE tenant_nominal_accounts ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_batches ADD COLUMN IF NOT EXISTS scan_mode VARCHAR(20) DEFAULT 'summary'",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS tenant_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS company_id UUID",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS supplier_posting_account VARCHAR(100)",
        "ALTER TABLE invoice_rows ADD COLUMN IF NOT EXISTS nominal_account_code VARCHAR(100)",
        "ALTER TABLE invoice_rows ALTER COLUMN method_used TYPE VARCHAR(200)",
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
    with engine.begin() as _conn:
        for _stmt in _RUNTIME_STMTS:
            try:
                _conn.execute(text(_stmt))
            except Exception:
                pass  # Statement already applied — safe to skip

    print("Database schema: OK")
except Exception as _schema_err:
    print(f"ERROR: Could not initialise database schema: {_schema_err}")
    print("  Check that DATABASE_URL points to a reachable PostgreSQL instance.")
    sys.exit(1)

# ── Now import the rest ───────────────────────────────────────────────────────
from app.db.session import SessionLocal
from app.db.models import Tenant, User, UserTenant
from app.utils.security import hash_password


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the first admin user and tenant for Invoice Scanner Service"
    )
    parser.add_argument("--admin-email",    required=True, help="Admin login email")
    parser.add_argument("--admin-password", required=True, help="Admin login password")
    parser.add_argument("--admin-name",     required=True, help="Admin full name")
    parser.add_argument("--tenant-code",    required=True, help="Unique tenant code, e.g. acme")
    parser.add_argument("--tenant-name",    required=True, help="Tenant display name")
    parser.add_argument("--contact-name",   default=None,  help="Tenant contact name")
    parser.add_argument("--contact-email",  default=None,  help="Tenant contact email")
    parser.add_argument("--tenant-notes",   default=None,  help="Optional tenant notes")
    parser.add_argument(
        "--link-admin-to-tenant",
        action="store_true",
        help="Also assign the admin user to the tenant as default access",
    )
    parser.add_argument(
        "--force-update-password",
        action="store_true",
        help="If the admin already exists, update the password hash",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        admin_email = args.admin_email.strip().lower()
        tenant_code = args.tenant_code.strip().lower()

        # ── Tenant ────────────────────────────────────────────────────────────
        tenant = db.query(Tenant).filter(Tenant.tenant_code == tenant_code).first()
        if tenant:
            print(f"Tenant already exists: {tenant.tenant_name} ({tenant.id})")
        else:
            tenant = Tenant(
                tenant_code=tenant_code,
                tenant_name=args.tenant_name.strip(),
                status="active",
                is_active=True,
                contact_name=args.contact_name.strip() if args.contact_name else None,
                contact_email=(
                    args.contact_email.strip().lower() if args.contact_email else None
                ),
                notes=args.tenant_notes,
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            print(f"Created tenant: {tenant.tenant_name} ({tenant.id})")

        # ── Admin user ────────────────────────────────────────────────────────
        user = db.query(User).filter(User.email == admin_email).first()
        if user:
            print(f"Admin user already exists: {user.email} ({user.id})")
            changed = False
            if user.role != "admin":
                user.role = "admin"
                changed = True
            if not user.is_active:
                user.is_active = True
                changed = True
            if args.admin_name and user.full_name != args.admin_name.strip():
                user.full_name = args.admin_name.strip()
                changed = True
            if args.force_update_password:
                user.password_hash = hash_password(args.admin_password)
                changed = True
            if changed:
                db.commit()
                db.refresh(user)
                print("Updated existing admin user details.")
        else:
            user = User(
                email=admin_email,
                full_name=args.admin_name.strip(),
                password_hash=hash_password(args.admin_password),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created admin user: {user.email} ({user.id})")

        # ── Tenant link ───────────────────────────────────────────────────────
        if args.link_admin_to_tenant:
            existing_link = (
                db.query(UserTenant)
                .filter(
                    UserTenant.user_id == user.id,
                    UserTenant.tenant_id == tenant.id,
                )
                .first()
            )
            if existing_link:
                print("Admin already linked to tenant.")
            else:
                has_default = (
                    db.query(UserTenant)
                    .filter(
                        UserTenant.user_id == user.id,
                        UserTenant.is_default.is_(True),
                    )
                    .first()
                )
                link = UserTenant(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    tenant_role="observer",
                    is_default=not bool(has_default),
                )
                db.add(link)
                db.commit()
                print("Linked admin to tenant.")

        # ── Summary ───────────────────────────────────────────────────────────
        print()
        print("=" * 50)
        print("Seed complete.")
        print(f"  Admin email : {user.email}")
        print(f"  Tenant code : {tenant.tenant_code}")
        print(f"  Tenant id   : {tenant.id}")
        print()
        print("Next step: log in with the admin email and password.")
        print("Then go to Settings → Companies to create your first company,")
        print("and Settings → Suppliers to set up your supplier list.")
        print("=" * 50)
        return 0

    except Exception as err:
        print(f"\nERROR during seed: {err}")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

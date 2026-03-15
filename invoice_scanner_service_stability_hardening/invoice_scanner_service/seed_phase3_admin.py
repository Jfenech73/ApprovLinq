from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is importable when running from the project folder.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal
from app.db.models import Tenant, User, UserTenant
from app.utils.security import hash_password


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the first admin user and first tenant for Invoice Scanner Service"
    )
    parser.add_argument("--admin-email", required=True, help="Admin login email")
    parser.add_argument("--admin-password", required=True, help="Admin login password")
    parser.add_argument("--admin-name", required=True, help="Admin full name")
    parser.add_argument("--tenant-code", required=True, help="Unique tenant code, e.g. acme")
    parser.add_argument("--tenant-name", required=True, help="Tenant display name")
    parser.add_argument("--contact-name", default=None, help="Tenant contact name")
    parser.add_argument("--contact-email", default=None, help="Tenant contact email")
    parser.add_argument("--tenant-notes", default=None, help="Optional tenant notes")
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
                contact_email=args.contact_email.strip().lower() if args.contact_email else None,
                notes=args.tenant_notes,
            )
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            print(f"Created tenant: {tenant.tenant_name} ({tenant.id})")

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

        if args.link_admin_to_tenant:
            existing_link = (
                db.query(UserTenant)
                .filter(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id)
                .first()
            )
            if existing_link:
                print("Admin already linked to tenant.")
            else:
                has_default = db.query(UserTenant).filter(UserTenant.user_id == user.id, UserTenant.is_default.is_(True)).first()
                link = UserTenant(
                    user_id=user.id,
                    tenant_id=tenant.id,
                    tenant_role="observer",
                    is_default=not bool(has_default),
                )
                db.add(link)
                db.commit()
                print("Linked admin to tenant.")

        print("\nSeed complete.")
        print(f"Admin email: {user.email}")
        print(f"Tenant code: {tenant.tenant_code}")
        print(f"Tenant id: {tenant.id}")
        print("Use the admin email/password on the login page.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

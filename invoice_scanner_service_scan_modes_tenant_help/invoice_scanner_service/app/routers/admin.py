from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    Tenant,
    User,
    UserTenant,
    Company,
    InvoiceBatch,
    InvoiceFile,
    InvoiceRow,
    IssueLog,
)
from app.db.session import get_db
from app.routers.auth import current_user
from app.schemas import (
    TenantCreate,
    TenantUpdate,
    TenantOut,
    UserCreate,
    UserUpdate,
    UserOut,
    IssueUpdate,
    IssueOut,
    CapacityRow,
)
from app.utils.security import hash_password

router = APIRouter(prefix="/admin", tags=["admin"])


VALID_STATUSES = {"active", "inactive"}
VALID_ROLES = {"admin", "tenant"}


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Tenant).order_by(Tenant.tenant_name.asc()).all()


@router.post("/tenants", response_model=TenantOut)
def create_tenant(payload: TenantCreate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    status = (payload.status or "active").strip().lower()
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Status must be active or inactive")

    tenant_code = payload.tenant_code.strip().lower()
    if db.query(Tenant).filter(Tenant.tenant_code == tenant_code).first():
        raise HTTPException(status_code=400, detail="Tenant code already exists")

    tenant = Tenant(
        tenant_code=tenant_code,
        tenant_name=payload.tenant_name.strip(),
        contact_name=payload.contact_name,
        contact_email=payload.contact_email,
        notes=payload.notes,
        status=status,
        is_active=status == "active",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.put("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(tenant_id: str, payload: TenantUpdate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    values = payload.model_dump(exclude_unset=True)
    if "status" in values and values["status"] is not None:
        status = values["status"].strip().lower()
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail="Status must be active or inactive")
        values["status"] = status
        values.setdefault("is_active", status == "active")

    for field, value in values.items():
        setattr(tenant, field, value)

    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/users", response_model=list[UserOut])
def list_users(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/users", response_model=UserOut)
def create_user(payload: UserCreate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    role = payload.role.strip().lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Role must be admin or tenant")

    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="User email already exists")

    if role != "admin" and not payload.tenant_ids:
        raise HTTPException(status_code=400, detail="Tenant users must be assigned to at least one tenant")

    if payload.tenant_ids:
        found = db.query(Tenant.id).filter(Tenant.id.in_(payload.tenant_ids)).all()
        if len(found) != len(set(payload.tenant_ids)):
            raise HTTPException(status_code=400, detail="One or more tenant assignments are invalid")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=role,
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    default_set = False
    for tenant_id in payload.tenant_ids:
        link = UserTenant(
            user_id=user.id,
            tenant_id=tenant_id,
            tenant_role="tenant_admin" if role != "admin" else "observer",
            is_default=not default_set,
        )
        db.add(link)
        default_set = True
    db.commit()
    return user


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: UserUpdate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    values = payload.model_dump(exclude_unset=True)
    tenant_ids = values.pop("tenant_ids", None)

    if "role" in values and values["role"] is not None:
        role = values["role"].strip().lower()
        if role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="Role must be admin or tenant")
        values["role"] = role

    for field, value in values.items():
        setattr(user, field, value)
    db.commit()

    if tenant_ids is not None:
        if user.role != "admin" and not tenant_ids:
            raise HTTPException(status_code=400, detail="Tenant users must be assigned to at least one tenant")
        if tenant_ids:
            found = db.query(Tenant.id).filter(Tenant.id.in_(tenant_ids)).all()
            if len(found) != len(set(tenant_ids)):
                raise HTTPException(status_code=400, detail="One or more tenant assignments are invalid")

        db.query(UserTenant).filter(UserTenant.user_id == user.id).delete()
        default_set = False
        for tenant_id in tenant_ids:
            db.add(UserTenant(
                user_id=user.id,
                tenant_id=tenant_id,
                tenant_role="tenant_admin" if user.role != "admin" else "observer",
                is_default=not default_set,
            ))
            default_set = True
        db.commit()

    db.refresh(user)
    return user


@router.get("/capacity", response_model=list[CapacityRow])
def capacity_usage(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenants = db.query(Tenant).order_by(Tenant.tenant_name.asc()).all()
    results: list[CapacityRow] = []
    for tenant in tenants:
        company_count = db.query(func.count(Company.id)).filter(Company.tenant_id == tenant.id).scalar() or 0
        batch_count = db.query(func.count(InvoiceBatch.id)).filter(InvoiceBatch.tenant_id == tenant.id).scalar() or 0
        file_count = db.query(func.count(InvoiceFile.id)).filter(InvoiceFile.tenant_id == tenant.id).scalar() or 0
        row_count = db.query(func.count(InvoiceRow.id)).filter(InvoiceRow.tenant_id == tenant.id).scalar() or 0
        storage_bytes = db.query(func.coalesce(func.sum(InvoiceFile.file_size_bytes), 0)).filter(InvoiceFile.tenant_id == tenant.id).scalar() or 0
        results.append(CapacityRow(
            tenant_id=tenant.id,
            tenant_name=tenant.tenant_name,
            tenant_code=tenant.tenant_code,
            status=tenant.status,
            companies=company_count,
            batches=batch_count,
            files=file_count,
            rows=row_count,
            storage_mb=round(storage_bytes / (1024 * 1024), 2),
        ))
    return results


@router.get("/issues", response_model=list[IssueOut])
def list_issues(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(IssueLog).order_by(IssueLog.updated_at.desc(), IssueLog.id.desc()).all()


@router.put("/issues/{issue_id}", response_model=IssueOut)
def update_issue(issue_id: int, payload: IssueUpdate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    issue = db.get(IssueLog, issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(issue, field, value)
    db.commit()
    db.refresh(issue)
    return issue

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


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Tenant).order_by(Tenant.tenant_name.asc()).all()


@router.post("/tenants", response_model=TenantOut)
def create_tenant(payload: TenantCreate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    tenant = Tenant(
        tenant_code=payload.tenant_code.strip().lower(),
        tenant_name=payload.tenant_name.strip(),
        contact_name=payload.contact_name,
        contact_email=payload.contact_email,
        notes=payload.notes,
        status="active",
        is_active=True,
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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/users", response_model=list[UserOut])
def list_users(_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/users", response_model=UserOut)
def create_user(payload: UserCreate, _user: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = User(
        email=payload.email.lower().strip(),
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
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
            tenant_role="tenant_admin" if payload.role != "admin" else "observer",
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
    for field, value in values.items():
        setattr(user, field, value)
    db.commit()

    if tenant_ids is not None:
        db.query(UserTenant).filter(UserTenant.user_id == user.id).delete()
        default_set = False
        for tenant_id in tenant_ids:
            db.add(UserTenant(user_id=user.id, tenant_id=tenant_id, tenant_role="tenant_admin", is_default=not default_set))
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

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Tenant, User, Company, TenantSupplier, TenantNominalAccount, IssueLog, UserTenant
from app.db.session import get_db
from app.routers.auth import current_tenant_id, current_user
from app.schemas import (
    TenantOut,
    TenantUpdate,
    CompanyCreate,
    CompanyUpdate,
    CompanyOut,
    SupplierCreate,
    SupplierUpdate,
    SupplierOut,
    NominalAccountCreate,
    NominalAccountUpdate,
    NominalAccountOut,
    IssueCreate,
    IssueOut,
)

router = APIRouter(prefix="/tenant", tags=["tenant"])


def require_tenant_user(user: User = Depends(current_user)) -> User:
    if user.role not in ("tenant", "admin"):
        raise HTTPException(status_code=403, detail="Tenant access required")
    return user


@router.get("/profile", response_model=TenantOut)
def get_profile(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.put("/profile", response_model=TenantOut)
def update_profile(payload: TenantUpdate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    safe_fields = {"tenant_name", "contact_name", "contact_email", "notes"}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in safe_fields:
            setattr(tenant, field, value)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/companies", response_model=list[CompanyOut])
def list_companies(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    return db.query(Company).filter(Company.tenant_id == tenant_id).order_by(Company.company_name.asc()).all()


@router.post("/companies", response_model=CompanyOut)
def create_company(payload: CompanyCreate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    company = Company(tenant_id=tenant_id, **payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.put("/companies/{company_id}", response_model=CompanyOut)
def update_company(company_id: str, payload: CompanyUpdate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if not company or company.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Company not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


@router.get("/suppliers", response_model=list[SupplierOut])
def list_suppliers(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    return db.query(TenantSupplier).filter(TenantSupplier.tenant_id == tenant_id).order_by(TenantSupplier.supplier_name.asc()).all()


@router.post("/suppliers", response_model=SupplierOut)
def create_supplier(payload: SupplierCreate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    supplier = TenantSupplier(tenant_id=tenant_id, **payload.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.put("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: int, payload: SupplierUpdate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    supplier = db.get(TenantSupplier, supplier_id)
    if not supplier or supplier.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("/nominal-accounts", response_model=list[NominalAccountOut])
def list_nominal_accounts(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    return db.query(TenantNominalAccount).filter(TenantNominalAccount.tenant_id == tenant_id).order_by(TenantNominalAccount.account_code.asc()).all()


@router.post("/nominal-accounts", response_model=NominalAccountOut)
def create_nominal_account(payload: NominalAccountCreate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    account = TenantNominalAccount(tenant_id=tenant_id, **payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/nominal-accounts/{account_id}", response_model=NominalAccountOut)
def update_nominal_account(account_id: int, payload: NominalAccountUpdate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    account = db.get(TenantNominalAccount, account_id)
    if not account or account.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Nominal account not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    db.commit()
    db.refresh(account)
    return account


@router.get("/issues", response_model=list[IssueOut])
def list_issues(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    return db.query(IssueLog).filter(IssueLog.tenant_id == tenant_id).order_by(IssueLog.updated_at.desc()).all()


@router.post("/issues", response_model=IssueOut)
def create_issue(payload: IssueCreate, tenant_id=Depends(current_tenant_id), user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    issue = IssueLog(tenant_id=tenant_id, created_by_user_id=user.id, **payload.model_dump())
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


@router.get("/users")
def list_tenant_users(tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    rows = (
        db.query(User, UserTenant)
        .join(UserTenant, UserTenant.user_id == User.id)
        .filter(UserTenant.tenant_id == tenant_id)
        .order_by(User.full_name.asc())
        .all()
    )
    return [
        {
            "user_id": str(user.id),
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role,
            "tenant_role": link.tenant_role,
            "is_active": user.is_active,
        }
        for user, link in rows
    ]

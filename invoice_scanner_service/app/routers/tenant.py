from __future__ import annotations

import csv
import io

from sqlalchemy.exc import IntegrityError

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
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


def _normalized_supplier_payload(payload: SupplierCreate | SupplierUpdate) -> dict:
    data = payload.model_dump(exclude_unset=True)
    supplier_account_code = (data.get("supplier_account_code") or data.get("posting_account") or "").strip() or None
    posting_account = (data.get("posting_account") or data.get("supplier_account_code") or "").strip() or None
    default_nominal = data.get("default_nominal")
    if isinstance(default_nominal, str):
        default_nominal = default_nominal.strip() or None
    if supplier_account_code is not None:
        data["supplier_account_code"] = supplier_account_code
    if posting_account is not None:
        data["posting_account"] = posting_account
    if "default_nominal" in data:
        data["default_nominal"] = default_nominal
    return data


def _header_key(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").replace("-", " ").split())


def _resolve_csv_header(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    resolved: dict[str, int] = {}
    for canonical, options in aliases.items():
        for option in options:
            key = _header_key(option)
            if key in header:
                resolved[canonical] = header.index(key)
                break
    missing = [canonical for canonical in aliases if canonical not in resolved]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"CSV header is missing required columns: {', '.join(missing)}",
        )
    return resolved


def _cell(row: list[str], idx: int) -> str:
    value = row[idx] if idx < len(row) else ""
    return value.strip() if isinstance(value, str) else ""


def _read_csv_upload(file: UploadFile) -> tuple[list[str], list[list[str]]]:
    filename = (file.filename or "").lower()
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file.")

    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The uploaded CSV file is empty.")

    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.") from exc

    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        raise HTTPException(status_code=400, detail="The uploaded CSV file is empty.")

    header = [_header_key(col) for col in rows[0]]
    data_rows = rows[1:] if header else rows
    return header, data_rows


@router.post("/suppliers", response_model=SupplierOut)
def create_supplier(payload: SupplierCreate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    supplier = TenantSupplier(tenant_id=tenant_id, **_normalized_supplier_payload(payload))
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.put("/suppliers/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: int, payload: SupplierUpdate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    supplier = db.get(TenantSupplier, supplier_id)
    if not supplier or supplier.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Supplier not found")
    for field, value in _normalized_supplier_payload(payload).items():
        setattr(supplier, field, value)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.post("/suppliers/import")
def import_suppliers(file: UploadFile = File(...), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    header, data_rows = _read_csv_upload(file)
    header_map = _resolve_csv_header(
        header,
        {
            "supplier account code": (
                "supplier account code",
                "supplier code",
                "account code",
                "posting account",
                "supplier account",
                "customer code",
            ),
            "supplier name": (
                "supplier name",
                "name",
                "supplier",
            ),
            "default nominal": (
                "default nominal",
                "nominal code",
                "nominal",
                "default nominal code",
            ),
        },
    )

    imported = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(data_rows, start=2):
        supplier_account_code = _cell(row, header_map["supplier account code"])
        supplier_name = _cell(row, header_map["supplier name"])
        default_nominal = _cell(row, header_map["default nominal"])

        if not supplier_account_code and not supplier_name and not default_nominal:
            continue
        if not supplier_account_code or not supplier_name:
            skipped += 1
            errors.append(f"Row {idx}: supplier account code and supplier name are required.")
            continue

        try:
            with db.begin_nested():
                existing_by_code = (
                    db.query(TenantSupplier)
                    .filter(
                        TenantSupplier.tenant_id == tenant_id,
                        TenantSupplier.supplier_account_code == supplier_account_code,
                    )
                    .first()
                )
                existing_by_name = (
                    db.query(TenantSupplier)
                    .filter(
                        TenantSupplier.tenant_id == tenant_id,
                        TenantSupplier.supplier_name.ilike(supplier_name),
                    )
                    .first()
                )

                if existing_by_code and existing_by_name and existing_by_code.id != existing_by_name.id:
                    skipped += 1
                    errors.append(
                        f"Row {idx}: supplier conflicts with existing records by code and name. Review this supplier manually."
                    )
                    continue

                existing = existing_by_code or existing_by_name

                if existing:
                    existing.supplier_account_code = supplier_account_code
                    existing.supplier_name = supplier_name
                    existing.default_nominal = default_nominal or None
                    existing.posting_account = supplier_account_code
                    existing.is_active = True
                else:
                    db.add(TenantSupplier(
                        tenant_id=tenant_id,
                        supplier_account_code=supplier_account_code,
                        supplier_name=supplier_name,
                        default_nominal=default_nominal or None,
                        posting_account=supplier_account_code,
                        is_active=True,
                    ))
                db.flush()
                imported += 1
        except IntegrityError:
            skipped += 1
            errors.append(f"Row {idx}: supplier could not be imported because it duplicates an existing record.")

    db.commit()
    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:20]}


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


@router.post("/nominal-accounts/import")
def import_nominal_accounts(file: UploadFile = File(...), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    header, data_rows = _read_csv_upload(file)
    header_map = _resolve_csv_header(
        header,
        {
            "nominal code": ("nominal code", "account code", "code"),
            "nominal account name": ("nominal account name", "account name", "nominal name", "name"),
        },
    )

    imported = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(data_rows, start=2):
        account_code = _cell(row, header_map["nominal code"])
        account_name = _cell(row, header_map["nominal account name"])

        if not account_code and not account_name:
            continue
        if not account_code or not account_name:
            skipped += 1
            errors.append(f"Row {idx}: nominal code and nominal account name are required.")
            continue

        try:
            with db.begin_nested():
                existing = (
                    db.query(TenantNominalAccount)
                    .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.account_code == account_code)
                    .first()
                )
                if existing:
                    existing.account_name = account_name
                    existing.is_active = True
                else:
                    db.add(TenantNominalAccount(
                        tenant_id=tenant_id,
                        account_code=account_code,
                        account_name=account_name,
                        is_active=True,
                    ))
                db.flush()
                imported += 1
        except IntegrityError:
            skipped += 1
            errors.append(f"Row {idx}: nominal account could not be imported because it duplicates an existing record.")

    db.commit()
    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:20]}


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

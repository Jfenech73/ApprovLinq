from __future__ import annotations

import csv
import io
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query
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


def _get_company_or_400(db: Session, tenant_id, company_id: UUID | None) -> Company:
    if not company_id:
        raise HTTPException(status_code=400, detail="Select a company first")
    company = db.get(Company, company_id)
    if not company or company.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Selected company does not belong to tenant")
    return company


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
def list_suppliers(company_id: UUID | None = Query(default=None), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    _get_company_or_400(db, tenant_id, company_id)
    return (
        db.query(TenantSupplier)
        .filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.company_id == company_id)
        .order_by(TenantSupplier.supplier_name.asc())
        .all()
    )


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
    return " ".join((value or "").strip().lower().replace("_", " ").replace("-", " ").replace("/", " ").split())


def _pick_column_map(header: list[str], aliases: dict[str, list[str]]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for idx, name in enumerate(header):
        for canonical, possible in aliases.items():
            if canonical in positions:
                continue
            if name == canonical or name in possible:
                positions[canonical] = idx
                break
    return positions


def _csv_value(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    value = row[idx]
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
    _get_company_or_400(db, tenant_id, payload.company_id)
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
def import_suppliers(file: UploadFile = File(...), company_id: UUID | None = Query(default=None), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    _get_company_or_400(db, tenant_id, company_id)
    header, data_rows = _read_csv_upload(file)
    aliases = {
        "supplier account code": [
            "supplier code", "account code", "supplier account", "supplier account number", "account number",
            "supplier posting account", "posting account", "customer code",
        ],
        "supplier name": ["name", "supplier", "vendor", "vendor name", "supplier description"],
        "default nominal": ["nominal", "nominal code", "default nominal code", "default account", "default gl", "gl code"],
    }
    columns = _pick_column_map(header, aliases)
    if "supplier account code" not in columns or "supplier name" not in columns:
        raise HTTPException(
            status_code=400,
            detail="Supplier CSV must include columns for supplier account code and supplier name. Optional: default nominal.",
        )

    imported = 0
    skipped = 0
    errors: list[str] = []
    seen_codes: set[str] = set()
    seen_names: set[str] = set()

    for idx, row in enumerate(data_rows, start=2):
        supplier_account_code = _csv_value(row, columns.get("supplier account code"))
        supplier_name = _csv_value(row, columns.get("supplier name"))
        default_nominal = _csv_value(row, columns.get("default nominal"))

        if not supplier_account_code and not supplier_name and not default_nominal:
            continue
        if not supplier_account_code or not supplier_name:
            skipped += 1
            errors.append(f"Row {idx}: supplier account code and supplier name are required.")
            continue

        code_key = supplier_account_code.casefold()
        name_key = supplier_name.casefold()
        if code_key in seen_codes or name_key in seen_names:
            skipped += 1
            errors.append(f"Row {idx}: duplicate supplier found in the import file and was skipped.")
            continue
        seen_codes.add(code_key)
        seen_names.add(name_key)

        existing = (
            db.query(TenantSupplier)
            .filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.company_id == company_id, TenantSupplier.supplier_account_code == supplier_account_code)
            .first()
        )
        if not existing:
            existing = (
                db.query(TenantSupplier)
                .filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.company_id == company_id, TenantSupplier.supplier_name.ilike(supplier_name))
                .first()
            )

        try:
            with db.begin_nested():
                if existing:
                    existing.supplier_account_code = supplier_account_code
                    existing.supplier_name = supplier_name
                    existing.default_nominal = default_nominal or None
                    existing.posting_account = supplier_account_code
                    existing.is_active = True
                else:
                    db.add(TenantSupplier(
                        tenant_id=tenant_id,
                        company_id=company_id,
                        supplier_account_code=supplier_account_code,
                        supplier_name=supplier_name,
                        default_nominal=default_nominal or None,
                        posting_account=supplier_account_code,
                        is_active=True,
                    ))
                db.flush()
            imported += 1
        except Exception:
            skipped += 1
            errors.append(f"Row {idx}: could not be imported because it conflicts with an existing supplier.")
            continue

    db.commit()
    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:20]}


@router.get("/nominal-accounts", response_model=list[NominalAccountOut])
def list_nominal_accounts(company_id: UUID | None = Query(default=None), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    _get_company_or_400(db, tenant_id, company_id)
    return (
        db.query(TenantNominalAccount)
        .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.company_id == company_id)
        .order_by(TenantNominalAccount.account_code.asc())
        .all()
    )


@router.post("/nominal-accounts", response_model=NominalAccountOut)
def create_nominal_account(payload: NominalAccountCreate, tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    _get_company_or_400(db, tenant_id, payload.company_id)
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
def import_nominal_accounts(file: UploadFile = File(...), company_id: UUID | None = Query(default=None), tenant_id=Depends(current_tenant_id), _user: User = Depends(require_tenant_user), db: Session = Depends(get_db)):
    _get_company_or_400(db, tenant_id, company_id)
    header, data_rows = _read_csv_upload(file)
    aliases = {
        "nominal code": ["account code", "code", "nominal", "gl code", "gl account"],
        "nominal account name": ["account name", "name", "description", "nominal name", "gl name"],
    }
    columns = _pick_column_map(header, aliases)
    if "nominal code" not in columns or "nominal account name" not in columns:
        raise HTTPException(
            status_code=400,
            detail="Nominal CSV must include columns for nominal code and nominal account name.",
        )

    imported = 0
    skipped = 0
    errors: list[str] = []
    seen_codes: set[str] = set()

    for idx, row in enumerate(data_rows, start=2):
        account_code = _csv_value(row, columns.get("nominal code"))
        account_name = _csv_value(row, columns.get("nominal account name"))

        if not account_code and not account_name:
            continue
        if not account_code or not account_name:
            skipped += 1
            errors.append(f"Row {idx}: nominal code and nominal account name are required.")
            continue
        if account_code.casefold() in seen_codes:
            skipped += 1
            errors.append(f"Row {idx}: duplicate nominal code found in the import file and was skipped.")
            continue
        seen_codes.add(account_code.casefold())

        existing = (
            db.query(TenantNominalAccount)
            .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.company_id == company_id, TenantNominalAccount.account_code == account_code)
            .first()
        )
        if existing:
            existing.account_name = account_name
            existing.is_active = True
        else:
            db.add(TenantNominalAccount(tenant_id=tenant_id, company_id=company_id, account_code=account_code, account_name=account_name, is_active=True))
        imported += 1

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
        {"user_id": str(user.id), "full_name": user.full_name, "email": user.email, "role": user.role, "tenant_role": link.tenant_role, "is_active": user.is_active}
        for user, link in rows
    ]

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import Company, User
from app.db.session import get_db
from app.routers.auth import current_tenant_id, current_user
from app.services import expense_insights

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _check_company(db: Session, tenant_id, company_id: UUID) -> Company:
    company = db.get(Company, company_id)
    if not company or company.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/summary")
def get_summary(
    company_id: UUID = Query(...),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.approved_summary(db, tenant_id=tenant_id, company_id=company_id)


@router.get("/monthly")
def get_monthly(
    company_id: UUID = Query(...),
    months: int = Query(default=12, ge=1, le=36),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.approved_monthly(db, tenant_id=tenant_id, company_id=company_id, months=months)


@router.get("/top-suppliers")
def get_top_suppliers(
    company_id: UUID = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.top_suppliers(db, tenant_id=tenant_id, company_id=company_id, limit=limit)


@router.get("/category-spend")
def get_category_spend(
    company_id: UUID = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.category_spend(db, tenant_id=tenant_id, company_id=company_id, limit=limit)


@router.get("/duplicate-exposure")
def get_duplicate_exposure(
    company_id: UUID = Query(...),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.duplicate_exposure(db, tenant_id=tenant_id, company_id=company_id)


@router.get("/vat-exceptions")
def get_vat_exceptions(
    company_id: UUID = Query(...),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.vat_exceptions(db, tenant_id=tenant_id, company_id=company_id)


@router.get("/credit-note-impact")
def get_credit_note_impact(
    company_id: UUID = Query(...),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.credit_note_impact(db, tenant_id=tenant_id, company_id=company_id)


@router.get("/variance")
def get_variance(
    company_id: UUID = Query(...),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    return expense_insights.variance(db, tenant_id=tenant_id, company_id=company_id)

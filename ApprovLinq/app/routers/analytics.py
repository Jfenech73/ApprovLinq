from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.db.models import Company, InvoiceRow, User
from app.db.session import get_db
from app.routers.auth import current_tenant_id, current_user

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

    base = db.query(InvoiceRow).filter(
        InvoiceRow.tenant_id == tenant_id,
        InvoiceRow.company_id == company_id,
    )

    total_rows = base.count()
    needs_review = base.filter(InvoiceRow.review_required.is_(True)).count()

    agg = (
        db.query(
            func.coalesce(func.sum(InvoiceRow.total_amount), 0).label("total_spend"),
            func.coalesce(func.avg(InvoiceRow.confidence_score), 0).label("avg_confidence"),
        )
        .filter(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.company_id == company_id,
        )
        .first()
    )

    distinct_suppliers = (
        db.query(func.count(func.distinct(InvoiceRow.supplier_name)))
        .filter(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.company_id == company_id,
            InvoiceRow.supplier_name.isnot(None),
        )
        .scalar()
        or 0
    )

    return {
        "total_rows": total_rows,
        "needs_review": needs_review,
        "total_spend": float(agg.total_spend) if agg else 0.0,
        "avg_confidence": float(agg.avg_confidence) if agg else 0.0,
        "distinct_suppliers": distinct_suppliers,
    }


@router.get("/monthly")
def get_monthly(
    company_id: UUID = Query(...),
    months: int = Query(default=12, ge=1, le=36),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)
    cutoff = date.today().replace(day=1) - timedelta(days=months * 31)

    # Use date_trunc to group by month — stored in a single Python variable so
    # SQLAlchemy emits the same SQL expression in SELECT and GROUP BY, avoiding
    # the "must appear in GROUP BY" error that arises from repeated to_char calls
    # with separate parameter placeholders.
    month_expr = func.date_trunc("month", InvoiceRow.invoice_date)

    rows = (
        db.query(
            month_expr.label("month"),
            func.coalesce(func.sum(InvoiceRow.net_amount), 0).label("net"),
            func.coalesce(func.sum(InvoiceRow.vat_amount), 0).label("vat"),
            func.coalesce(func.sum(InvoiceRow.total_amount), 0).label("total"),
            func.count(InvoiceRow.id).label("count"),
        )
        .filter(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.company_id == company_id,
            InvoiceRow.invoice_date.isnot(None),
            InvoiceRow.invoice_date >= cutoff,
        )
        .group_by(text("1"))
        .order_by(text("1"))
        .all()
    )

    return [
        {
            "month": r.month.strftime("%Y-%m") if r.month else None,
            "net": float(r.net),
            "vat": float(r.vat),
            "total": float(r.total),
            "count": r.count,
        }
        for r in rows
    ]


@router.get("/top-suppliers")
def get_top_suppliers(
    company_id: UUID = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user: User = Depends(current_user),
):
    _check_company(db, tenant_id, company_id)

    rows = (
        db.query(
            InvoiceRow.supplier_name,
            func.coalesce(func.sum(InvoiceRow.total_amount), 0).label("total"),
            func.count(InvoiceRow.id).label("count"),
        )
        .filter(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.company_id == company_id,
            InvoiceRow.supplier_name.isnot(None),
        )
        .group_by(InvoiceRow.supplier_name)
        .order_by(func.coalesce(func.sum(InvoiceRow.total_amount), 0).desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "supplier_name": r.supplier_name,
            "total": float(r.total),
            "count": r.count,
        }
        for r in rows
    ]

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.insight_models import ApprovedInvoiceFact
from app.services.approved_invoice_facts import latest_fact_query


def _month_expr(db: Session):
    if db.bind and db.bind.dialect.name == "sqlite":
        return func.strftime("%Y-%m", ApprovedInvoiceFact.invoice_date)
    return func.to_char(func.date_trunc("month", ApprovedInvoiceFact.invoice_date), "YYYY-MM")


def approved_summary(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    q = latest_fact_query(db, tenant_id, company_id)
    total_rows = q.count()
    agg = q.with_entities(
        func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).label("total_spend"),
        func.coalesce(func.avg(ApprovedInvoiceFact.confidence_score), 0).label("avg_confidence"),
        func.count(func.distinct(ApprovedInvoiceFact.canonical_supplier_name)).label("distinct_suppliers"),
    ).first()
    return {
        "total_rows": total_rows,
        "needs_review": 0,
        "total_spend": float(agg.total_spend) if agg else 0.0,
        "avg_confidence": float(agg.avg_confidence) if agg else 0.0,
        "distinct_suppliers": int(agg.distinct_suppliers or 0) if agg else 0,
    }


def approved_monthly(db: Session, *, tenant_id: Any, company_id: Any | None, months: int = 12) -> list[dict[str, Any]]:
    cutoff = date.today().replace(day=1) - timedelta(days=months * 31)
    month = _month_expr(db)
    rows = (
        latest_fact_query(db, tenant_id, company_id)
        .filter(ApprovedInvoiceFact.invoice_date.isnot(None), ApprovedInvoiceFact.invoice_date >= cutoff)
        .with_entities(
            month.label("month"),
            func.coalesce(func.sum(ApprovedInvoiceFact.reporting_net_amount), 0).label("net"),
            func.coalesce(func.sum(ApprovedInvoiceFact.reporting_vat_amount), 0).label("vat"),
            func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).label("total"),
            func.count(ApprovedInvoiceFact.id).label("count"),
        )
        .group_by(month)
        .order_by(month)
        .all()
    )
    return [{"month": r.month, "net": float(r.net), "vat": float(r.vat), "total": float(r.total), "count": r.count} for r in rows]


def top_suppliers(db: Session, *, tenant_id: Any, company_id: Any | None, limit: int = 10) -> list[dict[str, Any]]:
    rows = (
        latest_fact_query(db, tenant_id, company_id)
        .filter(ApprovedInvoiceFact.canonical_supplier_name.isnot(None))
        .with_entities(
            ApprovedInvoiceFact.canonical_supplier_name.label("supplier_name"),
            func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).label("total"),
            func.count(ApprovedInvoiceFact.id).label("count"),
        )
        .group_by(ApprovedInvoiceFact.canonical_supplier_name)
        .order_by(func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).desc())
        .limit(limit)
        .all()
    )
    return [{"supplier_name": r.supplier_name, "total": float(r.total), "count": r.count} for r in rows]


def category_spend(db: Session, *, tenant_id: Any, company_id: Any | None, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        latest_fact_query(db, tenant_id, company_id)
        .with_entities(
            func.coalesce(ApprovedInvoiceFact.category, "Uncategorised").label("category"),
            func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).label("total"),
            func.count(ApprovedInvoiceFact.id).label("count"),
        )
        .group_by(func.coalesce(ApprovedInvoiceFact.category, "Uncategorised"))
        .order_by(func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).desc())
        .limit(limit)
        .all()
    )
    return [{"category": r.category, "total": float(r.total), "count": r.count} for r in rows]


def duplicate_exposure(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    facts = latest_fact_query(db, tenant_id, company_id)
    exposed = facts.filter(ApprovedInvoiceFact.duplicate_exposure_count > 0)
    return {
        "exposed_count": exposed.count(),
        "exposed_total": float(exposed.with_entities(func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0)).scalar() or 0),
        "rows": [
            {
                "fact_id": fact.id,
                "invoice_number": fact.invoice_number,
                "supplier_name": fact.canonical_supplier_name,
                "total": float(fact.reporting_total_amount or 0),
                "duplicate_exposure_count": fact.duplicate_exposure_count,
                "duplicate_exposure_status": fact.duplicate_exposure_status,
            }
            for fact in exposed.order_by(ApprovedInvoiceFact.created_at.desc()).limit(50).all()
        ],
    }


def vat_exceptions(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    rows = []
    for fact in latest_fact_query(db, tenant_id, company_id).all():
        net = float(fact.reporting_net_amount or 0)
        vat = float(fact.reporting_vat_amount or 0)
        total = float(fact.reporting_total_amount or 0)
        if abs((net + vat) - total) > 0.02 and not fact.deposit_amount:
            rows.append({
                "fact_id": fact.id,
                "invoice_number": fact.invoice_number,
                "supplier_name": fact.canonical_supplier_name,
                "net": net,
                "vat": vat,
                "total": total,
                "difference": round(total - net - vat, 2),
            })
    return {"exception_count": len(rows), "rows": rows[:50]}


def credit_note_impact(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    facts = latest_fact_query(db, tenant_id, company_id).all()
    rows = []
    for fact in facts:
        doc = (fact.document_type or "").lower()
        total = float(fact.reporting_total_amount or 0)
        if "credit" in doc or total < 0:
            rows.append({
                "fact_id": fact.id,
                "invoice_number": fact.invoice_number,
                "supplier_name": fact.canonical_supplier_name,
                "document_type": fact.document_type,
                "total": total,
            })
    return {"credit_note_count": len(rows), "credit_note_total": round(sum(r["total"] for r in rows), 2), "rows": rows[:50]}


def variance(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    monthly = approved_monthly(db, tenant_id=tenant_id, company_id=company_id, months=13)
    rows = []
    prev_total = None
    for item in monthly:
        total = float(item["total"])
        change = None if prev_total in (None, 0) else round((total - prev_total) / prev_total, 4)
        rows.append({**item, "previous_total": prev_total, "change_ratio": change})
        prev_total = total
    return {"rows": rows}

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.insight_models import ApprovedInvoiceFact
from app.db.review_models import BatchExportEvent
from app.services.approved_invoice_facts import latest_fact_query


def _fact_ids_for_query(q) -> list[int]:
    return [int(row[0]) for row in q.with_entities(ApprovedInvoiceFact.id).order_by(ApprovedInvoiceFact.id).all()]


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
        "fact_ids": _fact_ids_for_query(q),
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
    out = []
    for r in rows:
        month_q = latest_fact_query(db, tenant_id, company_id).filter(
            ApprovedInvoiceFact.invoice_date.isnot(None),
            ApprovedInvoiceFact.invoice_date >= cutoff,
            month == r.month,
        )
        out.append({
            "month": r.month,
            "net": float(r.net),
            "vat": float(r.vat),
            "total": float(r.total),
            "count": r.count,
            "fact_ids": _fact_ids_for_query(month_q),
        })
    return out


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
    out = []
    for r in rows:
        supplier_q = latest_fact_query(db, tenant_id, company_id).filter(
            ApprovedInvoiceFact.canonical_supplier_name == r.supplier_name
        )
        out.append({
            "supplier_name": r.supplier_name,
            "total": float(r.total),
            "count": r.count,
            "fact_ids": _fact_ids_for_query(supplier_q),
        })
    return out


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
    out = []
    for r in rows:
        category_q = latest_fact_query(db, tenant_id, company_id).filter(
            func.coalesce(ApprovedInvoiceFact.category, "Uncategorised") == r.category
        )
        out.append({
            "category": r.category,
            "total": float(r.total),
            "count": r.count,
            "fact_ids": _fact_ids_for_query(category_q),
        })
    return out


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
                "evidence_ref_type": fact.evidence_ref_type,
                "evidence_ref_id": fact.evidence_ref_id,
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
                "evidence_ref_type": fact.evidence_ref_type,
                "evidence_ref_id": fact.evidence_ref_id,
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
                "evidence_ref_type": fact.evidence_ref_type,
                "evidence_ref_id": fact.evidence_ref_id,
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


def fact_drilldown(db: Session, *, tenant_id: Any, company_id: Any | None, fact_id: int) -> dict[str, Any]:
    q = latest_fact_query(db, tenant_id, company_id).filter(ApprovedInvoiceFact.id == fact_id)
    fact = q.first()
    if fact is None:
        raise LookupError("Approved invoice fact not found")

    export_event = db.get(BatchExportEvent, fact.export_event_id) if fact.export_event_id else None
    row = db.get(M.InvoiceRow, fact.source_row_id) if fact.source_row_id else None
    header = (
        db.query(M.InvoiceReadHeader)
        .filter(M.InvoiceReadHeader.row_id == fact.source_row_id)
        .order_by(M.InvoiceReadHeader.created_at.desc(), M.InvoiceReadHeader.id.desc())
        .first()
    )
    return {
        "fact": {
            "id": fact.id,
            "batch_id": str(fact.batch_id),
            "source_row_id": fact.source_row_id,
            "export_event_id": fact.export_event_id,
            "export_version": fact.export_version,
            "fact_version": fact.fact_version,
            "fact_fingerprint": fact.fact_fingerprint,
            "supplier_name": fact.canonical_supplier_name,
            "invoice_number": fact.invoice_number,
            "invoice_date": fact.invoice_date.isoformat() if fact.invoice_date else None,
            "net": float(fact.reporting_net_amount or 0),
            "vat": float(fact.reporting_vat_amount or 0),
            "total": float(fact.reporting_total_amount or 0),
            "currency": fact.reporting_currency or fact.currency,
        },
        "evidence": {
            "evidence_ref_type": fact.evidence_ref_type,
            "evidence_ref_id": fact.evidence_ref_id,
            "export_event": {
                "id": export_event.id,
                "export_version": export_event.export_version,
                "row_count": export_event.row_count,
                "has_file_bytes": bool(export_event.file_bytes),
                "file_path": export_event.file_path,
            } if export_event else None,
            "source_row": {
                "id": row.id,
                "row_status": row.row_status,
                "validation_status": row.validation_status,
                "review_required": row.review_required,
                "source_filename": row.source_filename,
                "page_no": row.page_no,
            } if row else None,
            "read_header": {
                "id": header.id,
                "provider_name": header.provider_name,
                "document_type": header.document_type,
                "source_filename": header.source_filename,
                "page_no": header.page_no,
            } if header else None,
        },
    }

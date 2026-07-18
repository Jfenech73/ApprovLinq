from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.insight_models import ApprovedInvoiceFact
from app.db.review_models import BatchExportEvent
from app.services.approved_invoice_facts import latest_fact_query


FACTS_VS_HYPOTHESES_CONTRACT = {
    "facts": "Metrics are computed from immutable approved invoice facts linked to export evidence.",
    "hypotheses": "Narrative observations are advisory and must not be treated as approved accounting facts.",
    "drilldown": "Every metric row exposes fact identifiers for source evidence review.",
}


def _fact_ids_for_query(q) -> list[int]:
    return [int(row[0]) for row in q.with_entities(ApprovedInvoiceFact.id).order_by(ApprovedInvoiceFact.id).all()]


def _month_expr(db: Session):
    if db.bind and db.bind.dialect.name == "sqlite":
        return func.strftime("%Y-%m", ApprovedInvoiceFact.invoice_date)
    return func.to_char(func.date_trunc("month", ApprovedInvoiceFact.invoice_date), "YYYY-MM")


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def exact_month_window(months: int, *, today: date | None = None) -> tuple[date, date]:
    if months < 1:
        raise ValueError("months must be at least 1")
    current = today or date.today()
    this_month = date(current.year, current.month, 1)
    end_exclusive = _add_months(this_month, 1)
    return _add_months(end_exclusive, -months), end_exclusive


def _with_contract(payload: dict[str, Any]) -> dict[str, Any]:
    payload["narrative_contract"] = dict(FACTS_VS_HYPOTHESES_CONTRACT)
    return payload


def approved_summary(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    q = latest_fact_query(db, tenant_id, company_id)
    total_rows = q.count()
    agg = q.with_entities(
        func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0).label("total_spend"),
        func.coalesce(func.avg(ApprovedInvoiceFact.confidence_score), 0).label("avg_confidence"),
        func.count(func.distinct(ApprovedInvoiceFact.canonical_supplier_name)).label("distinct_suppliers"),
    ).first()
    return _with_contract({
        "total_rows": total_rows,
        "needs_review": 0,
        "total_spend": float(agg.total_spend) if agg else 0.0,
        "avg_confidence": float(agg.avg_confidence) if agg else 0.0,
        "distinct_suppliers": int(agg.distinct_suppliers or 0) if agg else 0,
        "fact_ids": _fact_ids_for_query(q),
    })


def approved_monthly(db: Session, *, tenant_id: Any, company_id: Any | None, months: int = 12) -> list[dict[str, Any]]:
    start_inclusive, end_exclusive = exact_month_window(months)
    month = _month_expr(db)
    rows = (
        latest_fact_query(db, tenant_id, company_id)
        .filter(
            ApprovedInvoiceFact.invoice_date.isnot(None),
            ApprovedInvoiceFact.invoice_date >= start_inclusive,
            ApprovedInvoiceFact.invoice_date < end_exclusive,
        )
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
            ApprovedInvoiceFact.invoice_date >= start_inclusive,
            ApprovedInvoiceFact.invoice_date < end_exclusive,
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
    return _with_contract({
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
    })


def vat_exceptions(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    net = func.coalesce(ApprovedInvoiceFact.reporting_net_amount, 0)
    vat = func.coalesce(ApprovedInvoiceFact.reporting_vat_amount, 0)
    total = func.coalesce(ApprovedInvoiceFact.reporting_total_amount, 0)
    difference = total - net - vat
    q = latest_fact_query(db, tenant_id, company_id).filter(
        func.abs(difference) > 0.02,
        func.coalesce(ApprovedInvoiceFact.deposit_amount, 0) == 0,
    )
    rows = [
        {
            "fact_id": r.id,
            "invoice_number": r.invoice_number,
            "supplier_name": r.canonical_supplier_name,
            "net": float(r.net or 0),
            "vat": float(r.vat or 0),
            "total": float(r.total or 0),
            "difference": round(float(r.difference or 0), 2),
            "evidence_ref_type": r.evidence_ref_type,
            "evidence_ref_id": r.evidence_ref_id,
        }
        for r in q.with_entities(
            ApprovedInvoiceFact.id,
            ApprovedInvoiceFact.invoice_number,
            ApprovedInvoiceFact.canonical_supplier_name,
            net.label("net"),
            vat.label("vat"),
            total.label("total"),
            difference.label("difference"),
            ApprovedInvoiceFact.evidence_ref_type,
            ApprovedInvoiceFact.evidence_ref_id,
        ).order_by(func.abs(difference).desc()).limit(50).all()
    ]
    return _with_contract({"exception_count": q.count(), "rows": rows})


def credit_note_impact(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    total = func.coalesce(ApprovedInvoiceFact.reporting_total_amount, 0)
    q = latest_fact_query(db, tenant_id, company_id).filter(
        or_(
            func.lower(func.coalesce(ApprovedInvoiceFact.document_type, "")).like("%credit%"),
            total < 0,
        )
    )
    agg = q.with_entities(func.count(ApprovedInvoiceFact.id).label("count"), func.coalesce(func.sum(total), 0).label("total")).first()
    rows = [
        {
            "fact_id": fact.id,
            "invoice_number": fact.invoice_number,
            "supplier_name": fact.canonical_supplier_name,
            "document_type": fact.document_type,
            "total": float(fact.reporting_total_amount or 0),
            "evidence_ref_type": fact.evidence_ref_type,
            "evidence_ref_id": fact.evidence_ref_id,
        }
        for fact in q.order_by(ApprovedInvoiceFact.created_at.desc()).limit(50).all()
    ]
    return _with_contract({
        "credit_note_count": int(agg.count or 0) if agg else 0,
        "credit_note_total": round(float(agg.total or 0), 2) if agg else 0.0,
        "rows": rows,
    })


def variance(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[str, Any]:
    monthly = approved_monthly(db, tenant_id=tenant_id, company_id=company_id, months=13)
    rows = []
    prev_total = None
    for item in monthly:
        total = float(item["total"])
        change = None if prev_total in (None, 0) else round((total - prev_total) / prev_total, 4)
        rows.append({**item, "previous_total": prev_total, "change_ratio": change})
        prev_total = total
    return _with_contract({"rows": rows})


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
    return _with_contract({
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
            "source_currency": fact.currency,
            "fx_rate": float(fact.fx_rate or 0) if fact.fx_rate is not None else None,
            "fx_rate_source": fact.fx_rate_source,
            "fx_rate_date": fact.fx_rate_date.isoformat() if fact.fx_rate_date else None,
            "deposit_component_type": fact.deposit_component_type,
            "deposit_component_source": fact.deposit_component_source,
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
            "fx_rate_provenance": fact.fx_rate_provenance_json,
            "deposit_component_provenance": fact.deposit_component_provenance_json,
        },
    })

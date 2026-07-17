from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.insight_models import ApprovedInvoiceFact
from app.db.review_models import BatchExportEvent, InvoiceDuplicateCandidate
from app.services.export_eligibility import DEFAULT_EXPORT_ELIGIBILITY_POLICY, ExportEligibilityError


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _text(value: Any, limit: int | None = None) -> str | None:
    if value in (None, ""):
        return None
    out = " ".join(str(value).split())
    return out[:limit] if limit else out


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _latest_headers(db: Session, row_ids: list[int]) -> dict[int, M.InvoiceReadHeader]:
    if not row_ids:
        return {}
    headers = db.execute(
        select(M.InvoiceReadHeader)
        .where(M.InvoiceReadHeader.row_id.in_(row_ids))
        .order_by(M.InvoiceReadHeader.created_at.desc(), M.InvoiceReadHeader.id.desc())
    ).scalars().all()
    out: dict[int, M.InvoiceReadHeader] = {}
    for header in headers:
        if header.row_id and header.row_id not in out:
            out[header.row_id] = header
    return out


def _nominal_names(db: Session, tenant_id: Any, company_id: Any | None) -> dict[str, str]:
    q = select(M.TenantNominalAccount).where(
        M.TenantNominalAccount.tenant_id == tenant_id,
        M.TenantNominalAccount.is_active.is_(True),
    )
    if company_id is None:
        q = q.where(M.TenantNominalAccount.company_id.is_(None))
    else:
        q = q.where(M.TenantNominalAccount.company_id == company_id)
    out: dict[str, str] = {}
    for account in db.execute(q).scalars().all():
        code = str(account.account_code or "").strip()
        if code:
            out[code] = account.account_name
    return out


def _duplicate_exposure(db: Session, row_ids: list[int]) -> dict[int, tuple[int, str | None]]:
    if not row_ids:
        return {}
    records = db.execute(
        select(InvoiceDuplicateCandidate).where(InvoiceDuplicateCandidate.row_id.in_(row_ids))
    ).scalars().all()
    grouped: dict[int, list[InvoiceDuplicateCandidate]] = {}
    for record in records:
        grouped.setdefault(record.row_id, []).append(record)
    out: dict[int, tuple[int, str | None]] = {}
    for row_id, items in grouped.items():
        statuses = [item.match_status for item in items if item.match_status]
        status = "blocked_duplicate" if "blocked_duplicate" in statuses else statuses[0] if statuses else None
        out[row_id] = (len(items), status)
    return out


def _is_bcrs_fact(row_data: dict[str, Any]) -> bool:
    joined = " ".join(
        str(row_data.get(key) or "")
        for key in ("description", "method_used", "validation_status", "totals_reconciliation_status")
    ).lower()
    return bool(re.search(r"\b(bcrs|deposit|returnable|container)\b", joined))


def materialise_approved_invoice_facts_for_export(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    export_event: BatchExportEvent,
) -> int:
    """Persist immutable facts from the final corrected export snapshot."""
    from app.services.corrected_exporter import build_corrected_rows

    if export_event.id is None or export_event.batch_id != batch.id:
        raise ValueError("Approved facts require a persisted export event for the same batch")
    if not (export_event.file_bytes or export_event.file_path):
        raise ValueError("Approved facts require durable export-event evidence")
    eligibility = DEFAULT_EXPORT_ELIGIBILITY_POLICY.ensure_export_allowed(db, batch)
    rows = build_corrected_rows(db, batch)
    if not rows:
        return 0
    if export_event.row_count is not None and int(export_event.row_count) != eligibility.exportable_row_count:
        raise ExportEligibilityError(DEFAULT_EXPORT_ELIGIBILITY_POLICY.evaluate(db, batch))
    row_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
    headers = _latest_headers(db, row_ids)
    nominal_names = _nominal_names(db, batch.tenant_id, batch.company_id)
    duplicate_map = _duplicate_exposure(db, row_ids)
    existing = {
        row_id
        for (row_id,) in db.execute(
            select(ApprovedInvoiceFact.source_row_id).where(
                ApprovedInvoiceFact.batch_id == batch.id,
                ApprovedInvoiceFact.fact_version == export_event.export_version,
            )
        ).all()
    }

    created = 0
    export_total = Decimal("0.00")
    for row_data in rows:
        row_id = int(row_data["id"])
        if row_id in existing:
            continue
        if (row_data.get("row_status") or M.INVOICE_ROW_STATUS_ACTIVE) != M.INVOICE_ROW_STATUS_ACTIVE:
            continue
        header = headers.get(row_id)
        code = _text(row_data.get("nominal_account_code"), 100)
        nominal_name = nominal_names.get(code or "")
        currency = _text(row_data.get("currency"), 20)
        reporting_currency = currency
        total = _money(row_data.get("total_amount"))
        net = _money(row_data.get("net_amount"))
        vat = _money(row_data.get("vat_amount"))
        bcrs_amount = total if total is not None and _is_bcrs_fact(row_data) else None
        dup_count, dup_status = duplicate_map.get(row_id, (0, None))
        fingerprint_payload = {
            "tenant_id": str(batch.tenant_id),
            "company_id": str(batch.company_id) if batch.company_id else None,
            "batch_id": str(batch.id),
            "scan_run_id": str(row_data.get("scan_run_id") or ""),
            "source_row_id": row_id,
            "export_version": export_event.export_version,
            "supplier": row_data.get("supplier_name"),
            "invoice_number": row_data.get("invoice_number"),
            "invoice_date": row_data.get("invoice_date"),
            "net": str(net) if net is not None else None,
            "vat": str(vat) if vat is not None else None,
            "total": str(total) if total is not None else None,
            "currency": currency,
        }
        fact = ApprovedInvoiceFact(
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            batch_id=batch.id,
            scan_run_id=row_data.get("scan_run_id"),
            source_row_id=row_id,
            export_event_id=export_event.id,
            export_version=export_event.export_version,
            fact_version=export_event.export_version,
            fact_fingerprint=_fingerprint(fingerprint_payload),
            evidence_ref_type="batch_export_event",
            evidence_ref_id=str(export_event.id),
            canonical_supplier_name=_text(row_data.get("supplier_name")),
            supplier_posting_account=_text(row_data.get("supplier_posting_account"), 100),
            supplier_vat=_text(getattr(header, "supplier_vat", None) or getattr(header, "VendorTaxId", None), 100),
            document_type=_text(getattr(header, "document_type", None) or getattr(header, "DocType", None), 80),
            invoice_number=_text(row_data.get("invoice_number")),
            invoice_date=_date(row_data.get("invoice_date")),
            description=_text(row_data.get("description")),
            nominal_account_code=code,
            nominal_account_name=nominal_name,
            category=nominal_name or code,
            currency=currency,
            reporting_currency=reporting_currency,
            tax_code=_text(row_data.get("tax_code"), 50),
            net_amount=net,
            vat_amount=vat,
            total_amount=total,
            reporting_net_amount=net,
            reporting_vat_amount=vat,
            reporting_total_amount=total,
            bcrs_amount=bcrs_amount,
            deposit_amount=bcrs_amount,
            source_row_status=row_data.get("row_status") or M.INVOICE_ROW_STATUS_ACTIVE,
            source_validation_status=_text(row_data.get("validation_status"), 100),
            source_review_required=bool(row_data.get("review_required")),
            confidence_score=_money(row_data.get("confidence_score")),
            duplicate_exposure_count=dup_count,
            duplicate_exposure_status=dup_status,
        )
        db.add(fact)
        if total is not None:
            export_total += total
        created += 1
    if created:
        db.flush()
        fact_total = db.execute(
            select(func.coalesce(func.sum(ApprovedInvoiceFact.reporting_total_amount), 0)).where(
                ApprovedInvoiceFact.batch_id == batch.id,
                ApprovedInvoiceFact.export_event_id == export_event.id,
                ApprovedInvoiceFact.fact_version == export_event.export_version,
            )
        ).scalar() or Decimal("0.00")
        if Decimal(str(fact_total)).quantize(Decimal("0.01")) != export_total.quantize(Decimal("0.01")):
            raise ValueError("Approved fact total does not reconcile with corrected export snapshot")
    return created


def latest_fact_subquery(db: Session, tenant_id: Any, company_id: Any | None):
    criteria = [ApprovedInvoiceFact.tenant_id == tenant_id]
    if company_id is None:
        criteria.append(ApprovedInvoiceFact.company_id.is_(None))
    else:
        criteria.append(ApprovedInvoiceFact.company_id == company_id)
    return (
        db.query(
            ApprovedInvoiceFact.batch_id.label("batch_id"),
            func.max(ApprovedInvoiceFact.fact_version).label("fact_version"),
        )
        .filter(*criteria)
        .group_by(ApprovedInvoiceFact.batch_id)
        .subquery()
    )


def latest_fact_query(db: Session, tenant_id: Any, company_id: Any | None):
    latest = latest_fact_subquery(db, tenant_id, company_id)
    q = db.query(ApprovedInvoiceFact).join(
        latest,
        (ApprovedInvoiceFact.batch_id == latest.c.batch_id)
        & (ApprovedInvoiceFact.fact_version == latest.c.fact_version),
    )
    q = q.filter(ApprovedInvoiceFact.tenant_id == tenant_id)
    if company_id is None:
        q = q.filter(ApprovedInvoiceFact.company_id.is_(None))
    else:
        q = q.filter(ApprovedInvoiceFact.company_id == company_id)
    return q

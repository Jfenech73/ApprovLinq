"""Cross-batch duplicate detection for approved/exported invoice history.

This service runs after extraction.  It never changes extracted field values; it
only records duplicate evidence, marks rows for review, and soft-blocks export
when a conservative confidence threshold is met.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import (
    InvoiceDuplicateCandidate,
    InvoiceRowCorrection,
    InvoiceRowFieldAudit,
)
from app.services.supplier_history import normalise_supplier_key


STRONG_DUPLICATE_THRESHOLD = Decimal("0.90")
REVIEW_DUPLICATE_THRESHOLD = Decimal("0.70")


@dataclass(frozen=True)
class DuplicateIdentity:
    row_id: int
    batch_id: Any
    scan_run_id: Any
    invoice_number: str
    invoice_date: date | None
    total_cents: int | None
    currency: str
    supplier_key: str
    supplier_vat: str
    document_type: str
    fingerprint: str


@dataclass(frozen=True)
class DuplicateMatch:
    candidate_row: M.InvoiceRow
    candidate_batch: M.InvoiceBatch
    candidate_identity: DuplicateIdentity
    confidence: Decimal
    status: str
    evidence: dict[str, Any]


def _norm_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _currency(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


def _money_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        dec = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(dec * 100)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
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


def _header_value(header: M.InvoiceReadHeader | None, *names: str) -> Any:
    if header is None:
        return None
    for name in names:
        value = getattr(header, name, None)
        if value not in (None, ""):
            return value
    return None


def _final_value(row: M.InvoiceRow, correction: InvoiceRowCorrection | None, field: str) -> Any:
    if correction is not None:
        value = getattr(correction, field, None)
        if value not in (None, ""):
            return value
    return getattr(row, field, None)


def _identity(
    row: M.InvoiceRow,
    *,
    correction: InvoiceRowCorrection | None,
    header: M.InvoiceReadHeader | None,
) -> DuplicateIdentity:
    supplier_vat = _norm_token(_header_value(header, "supplier_vat", "VendorTaxId"))
    supplier_name = _final_value(row, correction, "supplier_name") or _header_value(header, "supplier_name", "VendorName")
    supplier_key = f"vat:{supplier_vat}" if len(supplier_vat) >= 4 else normalise_supplier_key(supplier_name)
    document_type = _norm_token(_header_value(header, "document_type", "DocType"))
    invoice_number = _norm_token(_final_value(row, correction, "invoice_number") or _header_value(header, "invoice_number", "InvoiceId"))
    invoice_date = _as_date(_final_value(row, correction, "invoice_date") or _header_value(header, "invoice_date", "InvoiceDate"))
    total_cents = _money_cents(_final_value(row, correction, "total_amount") or _header_value(header, "total_amount", "InvoiceTotal"))
    currency = _currency(_final_value(row, correction, "currency") or _header_value(header, "currency", "CurrencyCode"))
    fingerprint_source = "|".join([
        document_type,
        supplier_key,
        invoice_number,
        invoice_date.isoformat() if invoice_date else "",
        str(total_cents or ""),
        currency,
    ])
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:32] if fingerprint_source.strip("|") else ""
    return DuplicateIdentity(
        row_id=row.id,
        batch_id=row.batch_id,
        scan_run_id=getattr(row, "scan_run_id", None),
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        total_cents=total_cents,
        currency=currency,
        supplier_key=supplier_key,
        supplier_vat=supplier_vat,
        document_type=document_type,
        fingerprint=fingerprint,
    )


def _load_headers(db: Session, row_ids: list[int]) -> dict[int, M.InvoiceReadHeader]:
    if not row_ids:
        return {}
    rows = db.execute(
        select(M.InvoiceReadHeader)
        .where(M.InvoiceReadHeader.row_id.in_(row_ids))
        .order_by(M.InvoiceReadHeader.created_at.desc(), M.InvoiceReadHeader.id.desc())
    ).scalars().all()
    out: dict[int, M.InvoiceReadHeader] = {}
    for header in rows:
        if header.row_id and header.row_id not in out:
            out[header.row_id] = header
    return out


def _load_corrections(db: Session, row_ids: list[int]) -> dict[int, InvoiceRowCorrection]:
    if not row_ids:
        return {}
    rows = db.execute(
        select(InvoiceRowCorrection).where(InvoiceRowCorrection.row_id.in_(row_ids))
    ).scalars().all()
    return {row.row_id: row for row in rows}


def _score(current: DuplicateIdentity, candidate: DuplicateIdentity) -> tuple[Decimal, dict[str, Any]]:
    invoice_match = bool(current.invoice_number and current.invoice_number == candidate.invoice_number)
    date_match = bool(current.invoice_date and current.invoice_date == candidate.invoice_date)
    total_match = (
        current.total_cents is not None
        and candidate.total_cents is not None
        and abs(current.total_cents - candidate.total_cents) <= 1
    )
    currency_match = bool(current.currency and current.currency == candidate.currency)
    currency_compatible = currency_match or not current.currency or not candidate.currency
    supplier_match = bool(current.supplier_key and current.supplier_key == candidate.supplier_key)
    document_type_match = bool(current.document_type and current.document_type == candidate.document_type)
    fingerprint_match = bool(current.fingerprint and current.fingerprint == candidate.fingerprint)

    score = Decimal("0.00")
    score += Decimal("0.30") if invoice_match else Decimal("0.00")
    score += Decimal("0.20") if date_match else Decimal("0.00")
    score += Decimal("0.20") if total_match else Decimal("0.00")
    score += Decimal("0.10") if currency_match else Decimal("0.00")
    score += Decimal("0.15") if supplier_match else Decimal("0.00")
    score += Decimal("0.05") if document_type_match else Decimal("0.00")
    score += Decimal("0.10") if fingerprint_match else Decimal("0.00")
    score = min(score, Decimal("1.00"))

    core_match = invoice_match and date_match and total_match and currency_compatible
    strong = core_match and supplier_match and score >= STRONG_DUPLICATE_THRESHOLD
    review = core_match and score >= REVIEW_DUPLICATE_THRESHOLD
    status = "blocked_duplicate" if strong else "review_only" if review else "ignored"
    evidence = {
        "invoice_number_match": invoice_match,
        "invoice_date_match": date_match,
        "total_match": total_match,
        "currency_match": currency_match,
        "currency_compatible": currency_compatible,
        "supplier_match": supplier_match,
        "document_type_match": document_type_match,
        "document_fingerprint_match": fingerprint_match,
        "confidence": float(score),
        "threshold": float(STRONG_DUPLICATE_THRESHOLD),
    }
    return score, evidence | {"match_status": status}


def _append_token(value: str | None, token: str) -> str:
    parts = [p for p in re.split(r"[|]", value or "") if p]
    if token not in parts:
        parts.append(token)
    return "|".join(parts)


def _candidate_rows(db: Session, batch: M.InvoiceBatch) -> list[tuple[M.InvoiceRow, M.InvoiceBatch]]:
    criteria = [
        M.InvoiceRow.tenant_id == batch.tenant_id,
        M.InvoiceRow.batch_id != batch.id,
        M.InvoiceRow.row_status == M.INVOICE_ROW_STATUS_ACTIVE,
        func.lower(M.InvoiceBatch.status).in_(["approved", "exported"]),
        or_(
            M.InvoiceBatch.current_scan_run_id.is_(None),
            M.InvoiceRow.scan_run_id == M.InvoiceBatch.current_scan_run_id,
        ),
    ]
    if batch.company_id is None:
        criteria.append(M.InvoiceRow.company_id.is_(None))
    else:
        criteria.append(M.InvoiceRow.company_id == batch.company_id)
    return db.execute(
        select(M.InvoiceRow, M.InvoiceBatch)
        .join(M.InvoiceBatch, M.InvoiceBatch.id == M.InvoiceRow.batch_id)
        .where(*criteria)
        .order_by(M.InvoiceRow.created_at.desc(), M.InvoiceRow.id.desc())
        .limit(2000)
    ).all()


def _upsert_candidate(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    row: M.InvoiceRow,
    match: DuplicateMatch,
) -> InvoiceDuplicateCandidate:
    existing = db.execute(
        select(InvoiceDuplicateCandidate).where(
            InvoiceDuplicateCandidate.row_id == row.id,
            InvoiceDuplicateCandidate.candidate_row_id == match.candidate_row.id,
            InvoiceDuplicateCandidate.match_type == "cross_batch",
        )
    ).scalar_one_or_none()
    record = existing or InvoiceDuplicateCandidate(
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        batch_id=batch.id,
        scan_run_id=getattr(row, "scan_run_id", None),
        row_id=row.id,
        candidate_batch_id=match.candidate_batch.id,
        candidate_scan_run_id=getattr(match.candidate_row, "scan_run_id", None),
        candidate_row_id=match.candidate_row.id,
        match_type="cross_batch",
    )
    record.match_status = match.status
    record.confidence = match.confidence
    record.evidence_json = json.dumps(match.evidence, sort_keys=True, default=str)
    record.normalized_invoice_number = match.candidate_identity.invoice_number[:160] or None
    record.document_type = match.candidate_identity.document_type[:80] or None
    record.supplier_key = match.candidate_identity.supplier_key[:255] or None
    record.supplier_vat = match.candidate_identity.supplier_vat[:100] or None
    record.invoice_date = match.candidate_identity.invoice_date
    record.total_cents = match.candidate_identity.total_cents
    record.currency = match.candidate_identity.currency[:20] or None
    record.document_fingerprint = match.candidate_identity.fingerprint[:80] or None
    db.add(record)
    return record


def _apply_match(db: Session, batch: M.InvoiceBatch, row: M.InvoiceRow, match: DuplicateMatch) -> None:
    candidate_label = f"batch {match.candidate_batch.batch_name} row {match.candidate_row.id}"
    note = (
        f"Cross-batch duplicate candidate: {candidate_label}; "
        f"confidence={float(match.confidence):.2f}; status={match.status}."
    )
    row.review_required = True
    row.review_priority = "high"
    row.review_reasons = _append_token(
        row.review_reasons,
        f"possible_cross_batch_duplicate:row_{match.candidate_row.id}",
    )
    for field in ("invoice_number", "invoice_date", "total_amount", "currency"):
        row.review_fields = _append_token(row.review_fields, field)
    row.method_used = _append_token(row.method_used, "arbitrated:cross_batch_duplicate")

    old_status = getattr(row, "row_status", M.INVOICE_ROW_STATUS_ACTIVE) or M.INVOICE_ROW_STATUS_ACTIVE
    action = "cross_batch_duplicate_review"
    new_status = old_status
    if match.status == "blocked_duplicate":
        row.row_status = "blocked_duplicate"
        row.row_status_reason = "cross_batch_duplicate"
        row.row_status_note = note
        row.row_status_changed_at = datetime.utcnow()
        new_status = "blocked_duplicate"
        row.validation_status = "review_cross_batch_duplicate_blocked"
        action = "cross_batch_duplicate_block"
    else:
        row.validation_status = "review_cross_batch_duplicate"

    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        scan_run_id=getattr(row, "scan_run_id", None),
        row_id=row.id,
        field_name="_row",
        old_value=old_status,
        new_value=new_status,
        action=action,
        note=note,
        username="system",
    ))


def detect_cross_batch_duplicates(db: Session, batch: M.InvoiceBatch, scan_run_id=None) -> int:
    """Flag current-run rows that resemble prior approved/exported transactions."""
    if not batch or not batch.tenant_id:
        return 0
    row_query = select(M.InvoiceRow).where(
        M.InvoiceRow.batch_id == batch.id,
        M.InvoiceRow.row_status == M.INVOICE_ROW_STATUS_ACTIVE,
    )
    if scan_run_id is not None:
        row_query = row_query.where(M.InvoiceRow.scan_run_id == scan_run_id)
    current_rows = db.execute(row_query.order_by(M.InvoiceRow.id.asc())).scalars().all()
    if not current_rows:
        return 0

    candidates = _candidate_rows(db, batch)
    if not candidates:
        return 0

    all_row_ids = [row.id for row in current_rows] + [row.id for row, _batch in candidates]
    headers = _load_headers(db, all_row_ids)
    corrections = _load_corrections(db, all_row_ids)
    candidate_identities = [
        (
            candidate_row,
            candidate_batch,
            _identity(candidate_row, correction=corrections.get(candidate_row.id), header=headers.get(candidate_row.id)),
        )
        for candidate_row, candidate_batch in candidates
    ]

    changed = 0
    for row in current_rows:
        identity = _identity(row, correction=corrections.get(row.id), header=headers.get(row.id))
        if len(identity.invoice_number) < 3 or identity.invoice_date is None or identity.total_cents is None:
            continue
        best: DuplicateMatch | None = None
        for candidate_row, candidate_batch, candidate_identity in candidate_identities:
            confidence, evidence = _score(identity, candidate_identity)
            if evidence["match_status"] == "ignored":
                continue
            evidence.update({
                "candidate_batch_id": str(candidate_batch.id),
                "candidate_batch_name": candidate_batch.batch_name,
                "candidate_row_id": candidate_row.id,
                "candidate_scan_run_id": str(getattr(candidate_row, "scan_run_id", "") or ""),
                "current_row_id": row.id,
            })
            match = DuplicateMatch(
                candidate_row=candidate_row,
                candidate_batch=candidate_batch,
                candidate_identity=candidate_identity,
                confidence=confidence,
                status=evidence["match_status"],
                evidence=evidence,
            )
            if best is None or match.confidence > best.confidence:
                best = match
        if best is None:
            continue
        _upsert_candidate(db, batch=batch, row=row, match=best)
        _apply_match(db, batch, row, best)
        changed += 1

    if changed:
        db.commit()
    return changed

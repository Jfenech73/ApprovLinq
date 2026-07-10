"""Outcome labelling for persisted invoice field candidates.

Phase 8D records supervised-learning labels only after a row is explicitly
accepted by a reviewer or by export.  It does not influence extraction or
arbitration decisions.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import CORRECTABLE_FIELDS, InvoiceFieldCandidate, InvoiceRowCorrection
from app.services import correction_service as cs


_AMOUNT_FIELDS = {"net_amount", "vat_amount", "total_amount"}


def _value_to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
    return str(value).strip()


def normalise_outcome_value(field_name: str, value: Any) -> str:
    """Return a stable comparison key for candidate/final values."""
    if value is None:
        return ""
    if field_name in _AMOUNT_FIELDS:
        try:
            cleaned = str(value).replace(",", "").strip()
            return format(Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        except (InvalidOperation, ValueError):
            return str(value).strip().lower()
    if field_name == "invoice_date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        raw = str(value).strip()
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except Exception:
            return raw.lower()
    return " ".join(str(value).strip().lower().split())


def _final_field_value(row: M.InvoiceRow, correction: InvoiceRowCorrection | None, field_name: str) -> Any:
    if correction is not None:
        value = getattr(correction, field_name, None)
        if value is not None:
            return value
    return getattr(row, field_name, None)


def label_row_candidates(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    row: M.InvoiceRow,
    user: M.User | None = None,
    outcome_source: str,
) -> int:
    """Label persisted candidates for one explicitly accepted/reviewed row.

    Non-selected candidates keep rejected status; they receive final_value for
    later model training but are not marked accepted/corrected.
    """
    candidates = db.execute(
        select(InvoiceFieldCandidate).where(
            InvoiceFieldCandidate.batch_id == batch.id,
            InvoiceFieldCandidate.row_id == row.id,
            InvoiceFieldCandidate.tenant_id == batch.tenant_id,
            InvoiceFieldCandidate.scan_run_id == getattr(row, "scan_run_id", None),
        )
    ).scalars().all()
    if not candidates:
        return 0

    correction = db.get(InvoiceRowCorrection, row.id)
    now = datetime.utcnow()
    user_id = getattr(user, "id", None) if user is not None else None
    labelled = 0

    for cand in candidates:
        if cand.field_name not in CORRECTABLE_FIELDS:
            continue
        final_value = _final_field_value(row, correction, cand.field_name)
        final_text = _value_to_text(final_value)
        final_norm = normalise_outcome_value(cand.field_name, final_value)
        cand_norm = cand.normalised_value or normalise_outcome_value(cand.field_name, cand.candidate_value)

        cand.final_value = final_text
        cand.finalised_at = now
        cand.finalised_by = user_id
        cand.outcome_source = outcome_source[:40]
        if cand.selected:
            accepted = bool(cand_norm == final_norm)
            cand.user_accepted = accepted
            cand.user_corrected = not accepted
        else:
            # Rejected candidates remain rejected, but final_value is still useful
            # for later training and analytics.
            cand.user_accepted = False
            cand.user_corrected = False
        labelled += 1
    return labelled


def label_batch_candidates(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    user: M.User | None = None,
    outcome_source: str = "export",
) -> int:
    """Label candidates for all rows in a batch at export/acceptance time."""
    row_query = select(M.InvoiceRow).where(
        M.InvoiceRow.batch_id == batch.id,
        M.InvoiceRow.row_status == M.INVOICE_ROW_STATUS_ACTIVE,
    )
    if getattr(batch, "current_scan_run_id", None) is not None:
        row_query = row_query.where(M.InvoiceRow.scan_run_id == batch.current_scan_run_id)
    rows = db.execute(row_query).scalars().all()
    total = 0
    for row in rows:
        total += label_row_candidates(db, batch=batch, row=row, user=user, outcome_source=outcome_source)
    return total

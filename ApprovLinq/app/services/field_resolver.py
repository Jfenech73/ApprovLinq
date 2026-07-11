"""Field resolver facade.

The selected-value writer remains the existing deterministic arbitration
implementation.  This module is the Phase 7 service boundary used by scan and
manual replay flows.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow
from app.services.invoice_arbitration import ArbitrationResult, arbitrate_invoice_row


def resolve_invoice_row(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    extraction_payload: dict[str, Any] | None = None,
    *,
    context: dict[str, Any] | None = None,
) -> ArbitrationResult:
    """Resolve candidates into selected row values using existing arbitration."""
    return arbitrate_invoice_row(db, batch, row, extraction_payload, context=context)

"""Duplicate resolver boundary for Phase 7."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch
from app.services.cross_batch_duplicates import detect_cross_batch_duplicates


def detect_within_batch_duplicates(db: Session, batch_id: UUID, scan_run_id: Any = None) -> int:
    from app.routers.batches import _mark_duplicate_invoice_rows

    return _mark_duplicate_invoice_rows(db, batch_id, scan_run_id)


def detect_prior_batch_duplicates(db: Session, batch: InvoiceBatch, scan_run_id: Any = None) -> int:
    return detect_cross_batch_duplicates(db, batch, scan_run_id)

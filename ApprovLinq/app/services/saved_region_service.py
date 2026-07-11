"""Saved-region and saved-rule replay boundary for Phase 7."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow
from app.services.scan_performance import ScanPerformanceContext


def apply_saved_region_candidates(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    *,
    perf_ctx: ScanPerformanceContext | None = None,
    candidate_payload: dict | None = None,
) -> None:
    from app.routers.batches import _apply_remap_hints

    _apply_remap_hints(db, batch, row, perf_ctx=perf_ctx, candidate_payload=candidate_payload)


def apply_saved_rule_candidates(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    *,
    candidate_payload: dict | None = None,
) -> None:
    from app.routers.batches import _apply_saved_rules

    _apply_saved_rules(db, batch, row, candidate_payload=candidate_payload)

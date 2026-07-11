"""Amount/BCRS resolver boundary for Phase 7."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow


def decide_bcrs_split(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    payload: dict,
    page_rows: list[InvoiceRow] | None = None,
) -> tuple[str, float | None, str | None]:
    from app.routers.batches import _decide_bcrs_split

    return _decide_bcrs_split(db, batch, row, payload, page_rows)


def apply_bcrs_split(db: Session, row: InvoiceRow, amount: float) -> InvoiceRow:
    from app.routers.batches import _apply_bcrs_split

    return _apply_bcrs_split(db, row, amount)

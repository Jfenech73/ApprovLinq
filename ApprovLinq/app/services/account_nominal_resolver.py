"""Supplier account and nominal resolver boundary for Phase 7."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import InvoiceRow


def apply_master_data_enrichment(
    db: Session,
    tenant_id: Any,
    company_id: Any,
    row: InvoiceRow,
    *,
    supplier_vat: str | None = None,
    payload: dict | None = None,
) -> list[dict]:
    from app.routers.batches import _apply_master_data_enrichment

    return _apply_master_data_enrichment(
        db,
        tenant_id,
        company_id,
        row,
        supplier_vat=supplier_vat,
        payload=payload,
    )

"""Supplier identity resolver boundary for Phase 7.

Large supplier matching logic remains in the legacy batch helper for this phase;
callers use this module so the orchestration no longer depends directly on the
router implementation details.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow


def resolve_supplier_identity(
    db: Session,
    tenant_id: Any,
    company_id: Any,
    row: InvoiceRow,
    *,
    supplier_vat: str | None = None,
    batch: InvoiceBatch | None = None,
) -> dict[str, object]:
    from app.routers.batches import _resolve_supplier_identity

    return _resolve_supplier_identity(
        db,
        tenant_id,
        company_id,
        row,
        supplier_vat=supplier_vat,
        batch=batch,
    )

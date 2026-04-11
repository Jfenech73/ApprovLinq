"""Corrected-aware export wrapper.

Builds an export from a batch using corrected values (overlay), reuses the
existing workbook_from_rows() to keep main-sheet output byte-equivalent when
no corrections exist, then appends an 'Audit Changes' sheet and records a
BatchExportEvent. Status is moved to 'exported'.
"""
from __future__ import annotations
from copy import copy
from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import InvoiceRowFieldAudit, BatchExportEvent
from app.services import correction_service as cs
from app.services.exporter import workbook_from_rows, CORRECTABLE_FIELDS_ALIAS  # noqa: F401  (alias may not exist)


def _build_corrected_rows(db: Session, batch: M.InvoiceBatch) -> list[dict]:
    rows = db.execute(
        select(M.InvoiceRow).where(M.InvoiceRow.batch_id == batch.id)
        .order_by(M.InvoiceRow.source_file_id, M.InvoiceRow.page_no, M.InvoiceRow.id)
    ).scalars().all()
    cmap = cs.load_correction_map(db, batch.id)
    out = []
    for r in rows:
        c = cmap.get(r.id)
        d = {col: getattr(r, col) for col in r.__table__.columns.keys()}
        if c is not None:
            for f in (
                "supplier_name", "supplier_posting_account", "nominal_account_code",
                "invoice_number", "invoice_date", "description",
                "net_amount", "vat_amount", "total_amount", "currency", "tax_code",
            ):
                v = getattr(c, f, None)
                if v is not None:
                    d[f] = v
        out.append(d)
    return out


def export_batch_corrected(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    user: M.User,
    template_sheet=None,
    nominal_account_map: dict[str, str] | None = None,
    batch_metadata: dict | None = None,
) -> BytesIO:
    """Render the workbook with corrected values, append audit sheet, log event."""
    rows = _build_corrected_rows(db, batch)
    base_buf: BytesIO = workbook_from_rows(
        rows,
        batch_metadata=batch_metadata,
        nominal_account_map=nominal_account_map,
        template_sheet=template_sheet,
    )

    # Reopen workbook to append audit sheet
    base_buf.seek(0)
    wb = load_workbook(base_buf)
    ws = wb.create_sheet("Audit Changes")
    headers = ["Batch ID", "Export Version", "Exported At", "Row ID", "Field",
               "Original / Old", "New / Current", "Action", "User", "Note", "Changed At"]
    ws.append(headers)

    next_version = (getattr(batch, "current_export_version", 0) or 0) + 1
    now = datetime.utcnow().isoformat()

    audits = db.execute(
        select(InvoiceRowFieldAudit)
        .where(InvoiceRowFieldAudit.batch_id == batch.id)
        .order_by(InvoiceRowFieldAudit.created_at)
    ).scalars().all()
    for a in audits:
        ws.append([
            str(batch.id), next_version, now, a.row_id, a.field_name,
            a.old_value or "", a.new_value or "", a.action,
            a.username or (str(a.user_id) if a.user_id else ""),
            a.note or "", a.created_at.isoformat(),
        ])

    out = BytesIO()
    wb.save(out)
    out.seek(0)

    # Log export event + flip status + bump version
    ev = BatchExportEvent(
        batch_id=batch.id, export_version=next_version,
        exported_by=user.id, exported_at=datetime.utcnow(),
        row_count=len(rows),
    )
    db.add(ev)
    batch.current_export_version = next_version
    batch.exported_at = datetime.utcnow()
    batch.exported_by = user.id
    batch.status = "exported"
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id, row_id=0, field_name="__export__",
        old_value=None, new_value=f"v{next_version}", action="export",
        user_id=user.id,
    ))
    return out

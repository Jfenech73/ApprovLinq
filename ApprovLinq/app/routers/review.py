"""Review, correction, audit, remap, rules, reopen, preview routes."""
from __future__ import annotations
import io
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import (
    InvoiceRowCorrection, InvoiceRowFieldAudit, CorrectionRule, RemapHint, BatchExportEvent,
)
from app.db.session import get_db
from app.routers.auth import current_user
from app.services import correction_service as cs

router = APIRouter(prefix="/review", tags=["review"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class RowCorrectionIn(BaseModel):
    changes: dict[str, Any]
    note: str | None = None
    force_add: bool = False
    save_as_rule_fields: list[str] = []


class TransitionIn(BaseModel):
    target: str  # in_review | approved


class RemapIn(BaseModel):
    field_name: str
    page_no: int
    x: float
    y: float
    w: float
    h: float
    file_id: int | None = None


def _get_batch(db: Session, batch_id: UUID) -> M.InvoiceBatch:
    b = db.get(M.InvoiceBatch, batch_id)
    if not b:
        raise HTTPException(404, "Batch not found")
    return b


def _require_admin(user: M.User):
    if getattr(user, "role", None) != "admin":
        raise HTTPException(403, "Admin only")


# ── Review workspace ──────────────────────────────────────────────────────────
@router.get("/batches/{batch_id}")
def get_review_workspace(batch_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    rows = db.execute(select(M.InvoiceRow).where(M.InvoiceRow.batch_id == batch_id)
                      .order_by(M.InvoiceRow.source_file_id, M.InvoiceRow.page_no, M.InvoiceRow.id)).scalars().all()
    cmap = cs.load_correction_map(db, batch_id)
    out_rows = []
    corrected = 0
    flagged = 0
    for r in rows:
        c = cmap.get(r.id)
        eff = {f: cs.effective_value(r, c, f) for f in (
            "supplier_name", "supplier_posting_account", "nominal_account_code",
            "invoice_number", "invoice_date", "description",
            "net_amount", "vat_amount", "total_amount", "currency", "tax_code")}
        was_corrected = c is not None and any(getattr(c, f, None) is not None for f in eff)
        if was_corrected:
            corrected += 1
        if r.review_required:
            flagged += 1
        out_rows.append({
            "id": r.id,
            "source_filename": r.source_filename,
            "source_file_id": r.source_file_id,
            "page_no": r.page_no,
            "confidence_score": float(r.confidence_score) if r.confidence_score is not None else None,
            "review_required": r.review_required,
            "review_priority": r.review_priority,
            "review_fields": (r.review_fields or "").split(",") if r.review_fields else [],
            "row_reviewed": bool(c.row_reviewed) if c else False,
            "reviewed_fields": (c.reviewed_fields or "").split(",") if c and c.reviewed_fields else [],
            "is_corrected": was_corrected,
            "original": {f: getattr(r, f) for f in eff},
            "current": eff,
        })
    return {
        "batch": {
            "id": str(batch.id),
            "name": batch.batch_name,
            "status": cs.normalise_status(batch.status),
            "tenant_id": str(batch.tenant_id) if batch.tenant_id else None,
            "company_id": str(batch.company_id) if batch.company_id else None,
            "current_export_version": getattr(batch, "current_export_version", 0) or 0,
            "row_count": len(out_rows),
            "corrected_count": corrected,
            "flagged_count": flagged,
        },
        "rows": out_rows,
    }


# ── Edit / revert / mark reviewed ─────────────────────────────────────────────
@router.patch("/batches/{batch_id}/rows/{row_id}")
def save_corrections(batch_id: UUID, row_id: int, payload: RowCorrectionIn,
                     db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found in batch")
    try:
        audits = cs.apply_field_changes(
            db, batch=batch, row=row, changes=payload.changes, user=user,
            note=payload.note, force_add=payload.force_add,
            save_as_rule_fields=set(payload.save_as_rule_fields or []),
        )
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {"audited": len(audits)}


@router.post("/batches/{batch_id}/rows/{row_id}/revert/{field}")
def revert(batch_id: UUID, row_id: int, field: str,
           db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id) or HTTPException(404)
    if not isinstance(row, M.InvoiceRow) or row.batch_id != batch.id:
        raise HTTPException(404)
    a = cs.revert_field(db, batch=batch, row=row, field=field, user=user)
    db.commit()
    return {"reverted": bool(a)}


@router.get("/batches/{batch_id}/rows/{row_id}/audit")
def row_audit(batch_id: UUID, row_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    audits = db.execute(
        select(InvoiceRowFieldAudit)
        .where(InvoiceRowFieldAudit.batch_id == batch_id, InvoiceRowFieldAudit.row_id == row_id)
        .order_by(desc(InvoiceRowFieldAudit.created_at))
    ).scalars().all()
    return [{
        "id": a.id, "field": a.field_name, "old": a.old_value, "new": a.new_value,
        "action": a.action, "note": a.note, "rule_created": a.rule_created,
        "force_added": a.force_added, "user_id": str(a.user_id) if a.user_id else None,
        "username": a.username, "at": a.created_at.isoformat(),
    } for a in audits]


# ── Status transitions / reopen ───────────────────────────────────────────────
@router.post("/batches/{batch_id}/transition")
def transition(batch_id: UUID, payload: TransitionIn,
               db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    try:
        cs.transition_status(db, batch=batch, target=payload.target, user=user)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return {"status": batch.status}


@router.post("/batches/{batch_id}/reopen")
def reopen(batch_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    try:
        cs.reopen_batch(db, batch=batch, user=user)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return {"status": batch.status}


# ── PDF preview (on-demand, not stored) ───────────────────────────────────────
@router.get("/files/{file_id}/preview")
def preview(file_id: int, page: int = 1, db: Session = Depends(get_db), user=Depends(current_user)):
    f = db.get(M.InvoiceFile, file_id)
    if not f:
        raise HTTPException(404, "File not found")
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise HTTPException(501, "PyMuPDF not installed")
    try:
        doc = fitz.open(f.file_path)
        if page < 1 or page > doc.page_count:
            raise HTTPException(400, "Page out of range")
        pix = doc.load_page(page - 1).get_pixmap(dpi=110)
        png = pix.tobytes("png")
        doc.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Preview failed: {e}")
    return StreamingResponse(io.BytesIO(png), media_type="image/png")


# ── Remap hints ───────────────────────────────────────────────────────────────
@router.post("/batches/{batch_id}/rows/{row_id}/remap")
def save_remap(batch_id: UUID, row_id: int, payload: RemapIn,
               db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404)
    supplier = None
    if row.supplier_name:
        supplier = db.execute(
            select(M.TenantSupplier).where(
                M.TenantSupplier.tenant_id == batch.tenant_id,
                M.TenantSupplier.name == row.supplier_name,
            )
        ).scalar_one_or_none()
    hint = RemapHint(
        tenant_id=batch.tenant_id, company_id=batch.company_id,
        supplier_id=supplier.id if supplier else None,
        supplier_name_snapshot=row.supplier_name,
        field_name=payload.field_name, page_no=payload.page_no,
        x=payload.x, y=payload.y, w=payload.w, h=payload.h,
        source_batch_id=batch.id, source_file_id=payload.file_id or row.source_file_id,
        source_row_id=row.id, created_by=user.id,
    )
    db.add(hint)
    db.commit()
    return {"id": hint.id}


# ── Admin: rules ──────────────────────────────────────────────────────────────
@router.get("/admin/rules")
def list_rules(db: Session = Depends(get_db), user=Depends(current_user)):
    _require_admin(user)
    rules = db.execute(select(CorrectionRule).order_by(desc(CorrectionRule.created_at))).scalars().all()
    return [{
        "id": r.id, "tenant_id": str(r.tenant_id),
        "company_id": str(r.company_id) if r.company_id else None,
        "rule_type": r.rule_type, "field_name": r.field_name,
        "source_pattern": r.source_pattern, "target_value": r.target_value,
        "active": r.active, "created_at": r.created_at.isoformat(),
        "origin_batch_id": str(r.origin_batch_id) if r.origin_batch_id else None,
    } for r in rules]


@router.post("/admin/rules/{rule_id}/disable")
def disable_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    _require_admin(user)
    r = db.get(CorrectionRule, rule_id)
    if not r:
        raise HTTPException(404)
    r.active = False
    r.disabled_by = user.id
    r.disabled_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/admin/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    _require_admin(user)
    r = db.get(CorrectionRule, rule_id)
    if not r:
        raise HTTPException(404)
    db.delete(r)
    db.commit()
    return {"ok": True}

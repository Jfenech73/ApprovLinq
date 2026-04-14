"""Review, correction, audit, remap, rules, reopen, preview routes."""
from __future__ import annotations
import io
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
from app.utils.security import session_token_hash
from app.services import correction_service as cs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review", tags=["review"])


def current_user_flexible(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Resolve the current user from either the Authorization header (normal API
    calls) or a ?token=... query parameter (for <img src> requests, which cannot
    carry custom headers). Mirrors the logic in routers.auth.current_session."""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    else:
        bearer = token
    if not bearer:
        raise HTTPException(status_code=401, detail="Missing token")
    token_hash = session_token_hash(bearer)
    session_row = db.execute(
        select(M.UserSession).where(M.UserSession.token_hash == token_hash)
    ).scalar_one_or_none()
    if not session_row or (session_row.expires_at and session_row.expires_at < datetime.utcnow()):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(M.User, session_row.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


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
    apply_as_value: bool = False  # if True, also read text from region and return it


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


# ── Mark a file's flagged rows as reviewed (review-as-you-go) ────────────────
@router.post("/batches/{batch_id}/files/{file_id}/reviewed")
def mark_file_reviewed(batch_id: UUID, file_id: int,
                       db: Session = Depends(get_db), user=Depends(current_user)):
    """Create a zero-change correction record on every flagged row in this file
    that doesn't yet have one. This flips the file's review_state on /progress
    from 'needs_review' to 'reviewed' without requiring any edits."""
    batch = _get_batch(db, batch_id)
    f = db.get(M.InvoiceFile, file_id)
    if not f or f.batch_id != batch.id:
        raise HTTPException(404, "File not found in batch")
    rows = db.query(M.InvoiceRow).filter(M.InvoiceRow.source_file_id == file_id).all()
    flagged = [r for r in rows
               if (r.confidence_score is not None and float(r.confidence_score) < 0.55)
               or r.review_required]
    existing = {c.row_id for c in db.query(InvoiceRowCorrection).filter(
        InvoiceRowCorrection.row_id.in_([r.id for r in flagged])
    ).all()} if flagged else set()
    created = 0
    for r in flagged:
        if r.id in existing:
            # Already has a correction record — just ensure row_reviewed is True.
            corr = db.get(InvoiceRowCorrection, r.id)
            if corr and not corr.row_reviewed:
                corr.row_reviewed = True
                created += 1
            continue
        corr = InvoiceRowCorrection(row_id=r.id, batch_id=batch.id, row_reviewed=True)
        db.add(corr)
        db.flush()
        db.add(InvoiceRowFieldAudit(
            batch_id=batch.id, row_id=r.id, field_name="_file_reviewed",
            old_value=None, new_value="marked_reviewed",
            action="mark_reviewed", user_id=user.id, note=None,
        ))
        created += 1
    db.commit()
    return {"file_id": file_id, "marked_rows": created, "already_reviewed": len(flagged) - created}


# ── PDF file info (page count) ────────────────────────────────────────────────
def _open_pdf_page_count(path: str) -> int:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception as exc:
        logger.warning("pypdfium2 could not open '%s' for page count: %s", path, exc)
    try:
        import fitz
        doc = fitz.open(path)
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("PyMuPDF could not open '%s' for page count: %s", path, exc)
        return 1


@router.get("/files/{file_id}/info")
def file_info(
    file_id: int,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    current_user_flexible(token=token, authorization=authorization, db=db)
    f = db.get(M.InvoiceFile, file_id)
    if not f:
        raise HTTPException(404, "File not found")
    return {"file_id": file_id, "page_count": _open_pdf_page_count(f.file_path)}


# ── PDF preview (on-demand, not stored) ───────────────────────────────────────
@router.get("/files/{file_id}/preview")
def preview(
    file_id: int,
    page: int = 1,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    current_user_flexible(token=token, authorization=authorization, db=db)
    f = db.get(M.InvoiceFile, file_id)
    if not f:
        raise HTTPException(404, "File not found")
    import os
    if not os.path.exists(f.file_path):
        raise HTTPException(404, f"PDF missing from disk: {f.file_path}")
    # Prefer pypdfium2 (already a project dependency); fall back to PyMuPDF.
    errors = []
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(f.file_path)
        try:
            if page < 1 or page > len(pdf):
                raise HTTPException(400, "Page out of range")
            pg = pdf.get_page(page - 1)
            try:
                img = pg.render(scale=1.5).to_pil().convert("RGB")
            finally:
                pg.close()
        finally:
            pdf.close()
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return StreamingResponse(io.BytesIO(buf.getvalue()), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pypdfium2 preview failed for file %s (path=%s, page=%s): %s",
                     file_id, f.file_path, page, e)
        errors.append(f"pypdfium2: {e}")
    try:
        import fitz
        doc = fitz.open(f.file_path)
        if page < 1 or page > doc.page_count:
            doc.close()
            raise HTTPException(400, "Page out of range")
        pix = doc.load_page(page - 1).get_pixmap(dpi=110)
        png = pix.tobytes("png")
        doc.close()
        return StreamingResponse(io.BytesIO(png), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("PyMuPDF preview failed for file %s (path=%s, page=%s): %s",
                     file_id, f.file_path, page, e)
        errors.append(f"PyMuPDF: {e}")
    logger.error("Preview rendering failed completely for file %s path=%s: %s",
                 file_id, f.file_path, " | ".join(errors))
    raise HTTPException(500, "Preview rendering failed: " + " | ".join(errors))


# ── Read text from a region of a page ─────────────────────────────────────────
def _read_region_text(file_path: str, page_no: int, x: float, y: float, w: float, h: float) -> str:
    """Return text inside the normalized (0-1) rectangle on the page.
    Tries PyMuPDF's text layer first (digital PDFs); if empty or unavailable,
    falls back to rendering + OCR via the configured OCR backend."""
    # 1) PyMuPDF text-layer extraction (fast, free, exact)
    try:
        import fitz
        doc = fitz.open(file_path)
        try:
            if 1 <= page_no <= doc.page_count:
                page = doc.load_page(page_no - 1)
                pw, ph = page.rect.width, page.rect.height
                rect = fitz.Rect(x * pw, y * ph, (x + w) * pw, (y + h) * ph)
                text = (page.get_textbox(rect) or "").strip()
                if text:
                    return " ".join(text.split())
        finally:
            doc.close()
    except Exception:
        pass

    # 2) OCR fallback: render crop with pypdfium2 and POST to OCR.space
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # bundled with pypdfium2's deps
        pdf = pdfium.PdfDocument(file_path)
        try:
            if page_no < 1 or page_no > len(pdf):
                return ""
            pg = pdf.get_page(page_no - 1)
            try:
                full = pg.render(scale=3.0).to_pil().convert("RGB")
            finally:
                pg.close()
        finally:
            pdf.close()
        W, H = full.size
        box = (int(x * W), int(y * H), int((x + w) * W), int((y + h) * H))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return ""
        crop = full.crop(box)
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=80)
        img_bytes = buf.getvalue()
    except Exception:
        return ""

    try:
        from app.config import settings
        import requests
        if not getattr(settings, "ocr_space_api_key", None):
            return ""
        resp = requests.post(
            settings.ocr_space_endpoint,
            files={"file": ("region.jpg", img_bytes, "image/jpeg")},
            data={
                "apikey": settings.ocr_space_api_key,
                "language": settings.ocr_space_language,
                "isOverlayRequired": "false",
                "scale": "true",
                "OCREngine": str(settings.ocr_space_ocr_engine),
            },
            timeout=settings.ocr_space_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("IsErroredOnProcessing"):
            return ""
        out = []
        for item in payload.get("ParsedResults") or []:
            t = (item or {}).get("ParsedText") or ""
            if t:
                out.append(t)
        return " ".join(" ".join(out).split())
    except Exception:
        return ""


# ── Remap hints (and optional read-back) ──────────────────────────────────────
@router.post("/batches/{batch_id}/rows/{row_id}/remap")
def save_remap(batch_id: UUID, row_id: int, payload: RemapIn,
               db: Session = Depends(get_db), user=Depends(current_user)):
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404)
    supplier = None
    if row.supplier_name:
        supplier_q = (
            select(M.TenantSupplier).where(
                M.TenantSupplier.tenant_id == batch.tenant_id,
                M.TenantSupplier.supplier_name == row.supplier_name,
            )
        )
        if batch.company_id:
            supplier_q = supplier_q.where(M.TenantSupplier.company_id == batch.company_id)
        supplier = db.execute(supplier_q).scalar_one_or_none()
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

    # Optionally read the text inside the region and return it so the UI can
    # populate the corresponding field immediately.
    read_text = ""
    if payload.apply_as_value:
        file_id = payload.file_id or row.source_file_id
        f = db.get(M.InvoiceFile, file_id) if file_id else None
        if f:
            try:
                read_text = _read_region_text(
                    f.file_path, payload.page_no, payload.x, payload.y, payload.w, payload.h
                )
            except HTTPException:
                raise
            except Exception:
                read_text = ""
    return {"id": hint.id, "read_text": read_text}


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

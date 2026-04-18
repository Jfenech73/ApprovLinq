"""Review, correction, audit, remap, rules, reopen, preview routes."""
from __future__ import annotations
import io
import logging
import re
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
from app.utils.security import session_token_hash, utcnow
from app.services import correction_service as cs
from app.utils.storage import resolve_upload_path

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
    if session_row and session_row.expires_at:
        now = utcnow()
        expiry = session_row.expires_at
        if getattr(expiry, "tzinfo", None) is None and getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        elif getattr(expiry, "tzinfo", None) is not None and getattr(now, "tzinfo", None) is None:
            from datetime import timezone
            now = now.replace(tzinfo=timezone.utc)
        if expiry < now:
            session_row = None
    if not session_row:
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
    apply_as_value: bool = False   # if True, read text from region and persist it
    selected_text: str | None = None  # direct text selection from UI (preferred over OCR)


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
            "review_reasons": (r.review_reasons or "").split("|") if r.review_reasons else [],
            "method_used": r.method_used or "",
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


@router.post("/batches/{batch_id}/rows/{row_id}/duplicate")
def duplicate_row(batch_id: UUID, row_id: int,
                  db: Session = Depends(get_db), user=Depends(current_user)):
    """Create a manual duplicate of an invoice row for BCRS/deposit editing.

    The duplicate:
    - copies all invoice header fields (supplier, invoice number, date, source file)
    - sets amounts to zero so the reviewer can enter the correct deposit/BCRS values
    - is marked review_required=True with reason 'manually_duplicated_for_bcrs'
    - is included in export just like any other row
    """
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found in batch")

    from datetime import datetime as _dt
    duplicate = M.InvoiceRow(
        batch_id=row.batch_id,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        source_file_id=row.source_file_id,
        source_filename=row.source_filename,
        page_no=row.page_no,
        supplier_name=row.supplier_name,
        supplier_posting_account=row.supplier_posting_account,
        nominal_account_code=row.nominal_account_code,
        invoice_number=row.invoice_number,
        invoice_date=row.invoice_date,
        description=(f"{row.description or ''} - BCRS/Deposit (manual)").strip(" -"),
        line_items_raw=None,
        # Amounts zero — reviewer fills in the deposit/BCRS amount
        net_amount=0.0,
        vat_amount=0.0,
        total_amount=0.0,
        currency=row.currency,
        tax_code=row.tax_code,
        method_used="manual_duplicate",
        confidence_score=None,
        validation_status="manual",
        review_required=True,
        review_priority="high",
        review_reasons="manually_duplicated_for_bcrs",
        review_fields="net_amount|vat_amount|total_amount",
        auto_approved=False,
        page_quality_score=None,
        totals_raw=row.totals_raw,
        page_text_raw=row.page_text_raw,
        header_raw=row.header_raw,
    )
    db.add(duplicate)
    db.flush()  # get the new id

    # Audit the original row so history shows a duplicate was created
    from app.db.review_models import InvoiceRowFieldAudit
    audit = InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name="_action",
        old_value=None,
        new_value=f"duplicated → row {duplicate.id}",
        action="duplicate_created",
        note="Manual duplicate created for BCRS/deposit entry",
        user_id=user.id,
        username=getattr(user, "email", None) or str(user.id),
    )
    db.add(audit)
    db.commit()

    return {
        "duplicate_id": duplicate.id,
        "original_id":  row.id,
        "message": "Duplicate row created. Edit it to enter the BCRS/deposit amount, then save corrections.",
    }


class BcrsSplitIn(BaseModel):
    bcrs_amount: float


@router.post("/batches/{batch_id}/rows/{row_id}/bcrs_split")
def bcrs_split(batch_id: UUID, row_id: int, payload: BcrsSplitIn,
               db: Session = Depends(get_db), user=Depends(current_user)):
    """Create a BCRS split from the reviewer-supplied amount.

    Manual-correction path for cases where auto-detection missed or incorrectly
    picked the BCRS amount.

    - Creates a new BCRS row: net=bcrs_amount, vat=0, total=bcrs_amount.
    - Adjusts the source row total to net+vat (removes the BCRS component).
    - Records the total_amount correction via InvoiceRowCorrection (correct schema).
    - Writes InvoiceRowFieldAudit entries on both rows.
    - Returns JSON on both success and failure.
    """
    try:
        batch = _get_batch(db, batch_id)
        row = db.get(M.InvoiceRow, row_id)
        if not row or row.batch_id != batch.id:
            raise HTTPException(404, "Row not found in batch")

        amount = round(float(payload.bcrs_amount), 2)
        if amount <= 0:
            raise HTTPException(400, "bcrs_amount must be positive")
        row_total = round(float(row.total_amount or 0), 2)
        if row_total > 0 and amount >= row_total:
            raise HTTPException(400, "bcrs_amount must be less than the row total")

        # InvoiceRowCorrection and InvoiceRowFieldAudit are already imported at
        # the top of this module via the review_models import.

        # ── Build the BCRS row ───────────────────────────────────────────────
        desc = (row.description or "").strip()
        bcrs_desc = f"{desc} - BCRS" if desc and "bcrs" not in desc.lower() else (desc or "BCRS")
        bcrs_row = M.InvoiceRow(
            batch_id=row.batch_id,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            source_file_id=row.source_file_id,
            source_filename=row.source_filename,
            page_no=row.page_no,
            supplier_name=row.supplier_name,
            supplier_posting_account=row.supplier_posting_account,
            nominal_account_code=row.nominal_account_code,
            invoice_number=row.invoice_number,
            invoice_date=row.invoice_date,
            description=bcrs_desc,
            line_items_raw="BCRS surcharge",
            net_amount=amount,
            vat_amount=0.0,
            total_amount=amount,
            currency=row.currency,
            tax_code=row.tax_code,
            method_used=(row.method_used or "") + "+bcrs_manual",
            confidence_score=row.confidence_score,
            validation_status="manual",
            review_required=False,
            review_priority=None,
            review_reasons="manual_bcrs_split",
            review_fields=None,
            auto_approved=False,
            page_quality_score=row.page_quality_score,
            totals_raw=row.totals_raw,
            page_text_raw=row.page_text_raw,
            header_raw=row.header_raw,
        )
        db.add(bcrs_row)
        db.flush()  # get bcrs_row.id

        # ── Adjust the source row total ──────────────────────────────────────
        net = round(float(row.net_amount or 0), 2)
        vat = round(float(row.vat_amount or 0), 2)
        corrected_total = round(net + vat, 2)
        old_total = row_total
        row.total_amount = corrected_total
        row.review_required = False
        row.review_priority = None

        # ── Record correction via InvoiceRowCorrection (correct schema) ──────
        # InvoiceRowCorrection stores corrected field values directly — one
        # column per field, keyed by row_id (primary key).  There is no
        # field_name/original_value/corrected_value generic pair on this model.
        # Use get_or_create so we don't duplicate if a correction record already
        # exists for this row, then set total_amount + metadata.
        corr = cs.get_or_create_correction(db, row)
        corr.total_amount = corrected_total
        corr.updated_by = user.id
        from datetime import datetime as _dt
        corr.updated_at = _dt.utcnow()
        # Mark total_amount as a reviewed field so it surfaces correctly in UI
        existing_fields = set((corr.reviewed_fields or "").split(",")) - {""}
        existing_fields.add("total_amount")
        corr.reviewed_fields = ",".join(sorted(existing_fields))

        # ── Audit entries ────────────────────────────────────────────────────
        uname = getattr(user, "email", None) or str(user.id)
        db.add(InvoiceRowFieldAudit(
            batch_id=batch.id, row_id=row.id,
            field_name="total_amount",
            old_value=str(old_total),
            new_value=str(corrected_total),
            action="bcrs_split_source",
            note=f"Manual BCRS split: {amount:.2f} moved to new row {bcrs_row.id}",
            user_id=user.id, username=uname,
        ))
        db.add(InvoiceRowFieldAudit(
            batch_id=batch.id, row_id=bcrs_row.id,
            field_name="_action",
            old_value=None,
            new_value=f"bcrs_split ← row {row.id} (amount={amount:.2f})",
            action="bcrs_split_created",
            note=f"BCRS row created by manual split from row {row.id}",
            user_id=user.id, username=uname,
        ))
        db.commit()

        return {
            "bcrs_row_id":    bcrs_row.id,
            "original_id":    row.id,
            "bcrs_amount":    amount,
            "adjusted_total": corrected_total,
            "message": (
                f"BCRS split applied: BCRS row {bcrs_row.id} created for "
                f"{amount:.2f}; source row total adjusted to {corrected_total:.2f}."
            ),
        }

    except HTTPException:
        raise  # let FastAPI handle 400/404 normally — they already return JSON
    except Exception as exc:
        logger.exception("bcrs_split failed for batch=%s row=%s", batch_id, row_id)
        db.rollback()
        raise HTTPException(500, f"BCRS split failed: {exc!s:.200}")


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
    except Exception:
        pass
    try:
        import fitz
        doc = fitz.open(path)
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:
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
    file_path = resolve_upload_path(f.file_path)
    if not file_path.exists():
        raise HTTPException(404, f"PDF missing from disk: stored={f.file_path} resolved={file_path}")
    return {"file_id": file_id, "page_count": _open_pdf_page_count(str(file_path))}


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
    file_path = resolve_upload_path(f.file_path)
    if not file_path.exists():
        raise HTTPException(404, f"PDF missing from disk: stored={f.file_path} resolved={file_path}")
    errors = []
    try:
        import fitz
        doc = fitz.open(str(file_path))
        if page < 1 or page > doc.page_count:
            doc.close()
            raise HTTPException(400, "Page out of range")
        pix = doc.load_page(page - 1).get_pixmap(dpi=120, alpha=False)
        png = pix.tobytes("png")
        doc.close()
        return StreamingResponse(io.BytesIO(png), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        errors.append(f"PyMuPDF: {e}")
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(file_path))
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
        errors.append(f"pypdfium2: {e}")
    raise HTTPException(500, "Preview rendering failed. Tried: " + " | ".join(errors))


# ── Read text from a region of a page ─────────────────────────────────────────
def _count_meaningful(text: str) -> int:
    """Count alphanumeric chars — used to gate text-layer results vs junk/artefacts."""
    import re as _re
    return len(_re.findall(r"[A-Za-z0-9]", text or ""))


def _read_region_text(file_path: str, page_no: int, x: float, y: float, w: float, h: float) -> str:
    """Return the best text found inside the normalised (0-1) rectangle on the page.

    Resolution order — first tier yielding >= 2 meaningful alphanumeric chars wins.
    A bad/sparse text layer does NOT suppress later tiers.

    Tier 1  PyMuPDF get_textbox()         — fast, layout-aware, text PDFs
    Tier 2  pypdfium2 get_text_bounded()  — independent parser, catches what fitz misses
    Tier 3  Cropped-region OCR (pypdfium2 render + OCR.space)  — scanned/image pages

    Tier 1 and 2 are both text-layer methods but use different PDF parsers; either
    may succeed where the other returns garbage on a malformed/low-quality stream.
    Tier 3 never reruns whole-document OCR — it renders only the selected crop.
    """
    logger.debug(
        "_read_region_text: file=%s page=%d region=(%.3f,%.3f,%.3f,%.3f)",
        file_path, page_no, x, y, w, h,
    )

    # ── Tier 1: PyMuPDF get_textbox ───────────────────────────────────────
    try:
        import fitz
        doc = fitz.open(file_path)
        try:
            if 1 <= page_no <= doc.page_count:
                page = doc.load_page(page_no - 1)
                pw, ph = page.rect.width, page.rect.height
                rect = fitz.Rect(x * pw, y * ph, (x + w) * pw, (y + h) * ph)
                t1 = " ".join((page.get_textbox(rect) or "").split())
                m1 = _count_meaningful(t1)
                logger.debug("_read_region_text: tier1 (fitz) %r meaningful=%d", t1[:60], m1)
                if m1 >= 2:
                    logger.debug("_read_region_text: tier1 accepted → %r", t1[:80])
                    return t1
                logger.debug(
                    "_read_region_text: tier1 sparse (%d meaningful chars) → tier2", m1
                )
        finally:
            doc.close()
    except Exception as _e1:
        logger.debug("_read_region_text: tier1 (fitz) failed: %s", _e1)

    # ── Tier 2: pypdfium2 get_text_bounded (independent text-layer parser) ─
    try:
        import pypdfium2 as _pdfium2
        _pdf2 = _pdfium2.PdfDocument(file_path)
        try:
            if 1 <= page_no <= len(_pdf2):
                _pg2 = _pdf2.get_page(page_no - 1)
                try:
                    _tp = _pg2.get_textpage()
                    try:
                        _pw2 = _pg2.get_width()
                        _ph2 = _pg2.get_height()
                        # pypdfium2 PDF coords: y=0 at bottom, y=height at top
                        _left   = x * _pw2
                        _bottom = (1.0 - (y + h)) * _ph2
                        _right  = (x + w) * _pw2
                        _top    = (1.0 - y) * _ph2
                        t2 = " ".join((_tp.get_text_bounded(
                            left=_left, bottom=_bottom,
                            right=_right, top=_top,
                        ) or "").split())
                        m2 = _count_meaningful(t2)
                        logger.debug(
                            "_read_region_text: tier2 (pypdfium2 textpage) %r meaningful=%d",
                            t2[:60], m2,
                        )
                        if m2 >= 2:
                            logger.debug("_read_region_text: tier2 accepted → %r", t2[:80])
                            return t2
                        logger.debug(
                            "_read_region_text: tier2 sparse (%d) → tier3 (OCR)", m2
                        )
                    finally:
                        _tp.close()
                finally:
                    _pg2.close()
        finally:
            _pdf2.close()
    except Exception as _e2:
        logger.debug("_read_region_text: tier2 (pypdfium2 textpage) failed: %s", _e2)

    # ── Tier 3: cropped-region render + OCR (last resort) ─────────────────
    logger.debug("_read_region_text: tier3 — rendering crop for OCR")
    _img_bytes: bytes | None = None
    try:
        import pypdfium2 as _pdfium3
        _pdf3 = _pdfium3.PdfDocument(file_path)
        try:
            if page_no < 1 or page_no > len(_pdf3):
                return ""
            _pg3 = _pdf3.get_page(page_no - 1)
            try:
                _full = _pg3.render(scale=3.0).to_pil().convert("RGB")
            finally:
                _pg3.close()
        finally:
            _pdf3.close()
        _W, _H = _full.size
        _box = (int(x * _W), int(y * _H), int((x + w) * _W), int((y + h) * _H))
        if _box[2] - _box[0] < 4 or _box[3] - _box[1] < 4:
            logger.debug("_read_region_text: tier3 region too small — giving up")
            return ""
        _crop = _full.crop(_box)
        _buf = io.BytesIO()
        _crop.save(_buf, format="JPEG", quality=85)
        _img_bytes = _buf.getvalue()
        logger.debug(
            "_read_region_text: tier3 crop %dx%d px (%d bytes)",
            _box[2] - _box[0], _box[3] - _box[1], len(_img_bytes),
        )
    except Exception as _e3a:
        logger.debug("_read_region_text: tier3 render failed: %s", _e3a)
        return ""

    if not _img_bytes:
        return ""

    try:
        from app.config import settings
        import requests
        if not getattr(settings, "ocr_space_api_key", None):
            logger.debug("_read_region_text: tier3 skipped — OCR.space not configured")
            return ""
        _resp = requests.post(
            settings.ocr_space_endpoint,
            files={"file": ("region.jpg", _img_bytes, "image/jpeg")},
            data={
                "apikey":            settings.ocr_space_api_key,
                "language":          settings.ocr_space_language,
                "isOverlayRequired": "false",
                "scale":             "true",
                "OCREngine":         str(settings.ocr_space_ocr_engine),
            },
            timeout=settings.ocr_space_timeout_seconds,
        )
        _resp.raise_for_status()
        _ocr = _resp.json()
        if _ocr.get("IsErroredOnProcessing"):
            logger.debug("_read_region_text: tier3 OCR.space errored")
            return ""
        _out: list[str] = []
        for _item in _ocr.get("ParsedResults") or []:
            _t = (_item or {}).get("ParsedText") or ""
            if _t:
                _out.append(_t)
        t3 = " ".join(" ".join(_out).split())
        logger.debug(
            "_read_region_text: tier3 (OCR.space) %r meaningful=%d",
            t3[:60], _count_meaningful(t3),
        )
        return t3
    except Exception as _e3b:
        logger.debug("_read_region_text: tier3 OCR.space failed: %s", _e3b)
        return ""

# ── Remap hints + value persistence + rule creation ─────────────────────────
@router.post("/batches/{batch_id}/rows/{row_id}/remap")
def save_remap(batch_id: UUID, row_id: int, payload: RemapIn,
               db: Session = Depends(get_db), user=Depends(current_user)):
    """Apply a region remap to a row field.

    Pipeline:
    1. Upsert a RemapHint (stores bounding-box coordinates for future replay).
    2. Resolve the text in the selected region:
       - Use payload.selected_text directly when the UI sent a text-layer selection.
       - Otherwise call _read_region_text() (PyMuPDF text-layer → OCR fallback).
    3. If text was resolved AND apply_as_value is True:
       a. Persist the value into InvoiceRowCorrection immediately (no manual Save step).
       b. Write an InvoiceRowFieldAudit entry.
       c. Upsert a CorrectionRule(rule_type="remap_field_value") for future invoices.
    4. If no text could be resolved, return an explicit error so the UI shows a
       useful message instead of silently doing nothing.
    """
    logger.debug(
        "save_remap called: batch=%s row=%d field=%r page=%d coords=(%.3f,%.3f,%.3f,%.3f) "
        "selected_text=%r apply_as_value=%s",
        batch_id, row_id, payload.field_name, payload.page_no,
        payload.x, payload.y, payload.w, payload.h,
        (payload.selected_text or "")[:60], payload.apply_as_value,
    )

    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found in batch")

    # ── 1. Upsert RemapHint (coordinate region for future replay) ────────────
    supplier = None
    if row.supplier_name:
        supplier_q = select(M.TenantSupplier).where(
            M.TenantSupplier.tenant_id == batch.tenant_id,
            M.TenantSupplier.supplier_name == row.supplier_name,
        )
        if batch.company_id:
            supplier_q = supplier_q.where(M.TenantSupplier.company_id == batch.company_id)
        supplier = db.execute(supplier_q).scalar_one_or_none()

    existing_hint = db.execute(
        select(RemapHint).where(
            RemapHint.tenant_id == batch.tenant_id,
            RemapHint.field_name == payload.field_name,
            RemapHint.supplier_name_snapshot == row.supplier_name,
            RemapHint.page_no == payload.page_no,
        ).limit(1)
    ).scalar_one_or_none()

    if existing_hint:
        existing_hint.x = payload.x
        existing_hint.y = payload.y
        existing_hint.w = payload.w
        existing_hint.h = payload.h
        existing_hint.active = True
        existing_hint.source_batch_id = batch.id
        existing_hint.source_file_id  = payload.file_id or row.source_file_id
        existing_hint.source_row_id   = row.id
        if supplier:
            existing_hint.supplier_id = supplier.id
        hint = existing_hint
        logger.debug("save_remap: updated existing RemapHint id=%d", hint.id)
    else:
        hint = RemapHint(
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            supplier_id=supplier.id if supplier else None,
            supplier_name_snapshot=row.supplier_name,
            field_name=payload.field_name,
            page_no=payload.page_no,
            x=payload.x, y=payload.y, w=payload.w, h=payload.h,
            source_batch_id=batch.id,
            source_file_id=payload.file_id or row.source_file_id,
            source_row_id=row.id,
            created_by=user.id,
        )
        db.add(hint)
        logger.debug("save_remap: created new RemapHint for supplier=%r field=%r",
                     row.supplier_name, payload.field_name)

    # Flush (not commit) so hint gets its id but we can still roll back if
    # the rest of the pipeline fails.
    db.flush()

    # ── 2. Resolve text ───────────────────────────────────────────────────────
    # Priority order:
    #   a) Direct text selection sent by the UI (payload.selected_text)
    #   b) PyMuPDF text-layer extraction from the bounding box
    #   c) OCR fallback via _read_region_text (renders crop → OCR.space)
    read_text = ""

    if payload.selected_text and payload.selected_text.strip():
        # UI sent a text-layer selection — use it directly (most accurate)
        read_text = " ".join(payload.selected_text.strip().split())
        logger.debug("save_remap: using UI-provided selected_text=%r", read_text[:80])
    elif payload.apply_as_value:
        file_id = payload.file_id or row.source_file_id
        f = db.get(M.InvoiceFile, file_id) if file_id else None
        if f:
            try:
                raw = _read_region_text(
                    str(resolve_upload_path(f.file_path)),
                    payload.page_no,
                    payload.x, payload.y, payload.w, payload.h,
                )
                read_text = (raw or "").strip()
                logger.debug("save_remap: _read_region_text returned %r", read_text[:80])
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("save_remap: _read_region_text failed: %s", exc)
                read_text = ""
        else:
            logger.warning("save_remap: file not found for file_id=%s", file_id)

    # Normalise: collapse whitespace, strip leading/trailing
    read_text = " ".join(read_text.split()).strip() if read_text else ""
    logger.debug(
        "save_remap: resolved text=%r field=%r supplier=%r",
        read_text[:80], payload.field_name, row.supplier_name,
    )

    # If apply_as_value was requested but we could not resolve any text,
    # return an explicit error rather than silently succeeding.
    if payload.apply_as_value and not read_text:
        # Still commit the RemapHint so the region is saved for later
        db.commit()
        logger.warning(
            "save_remap: could not resolve text from region "
            "field=%r page=%d coords=(%.3f,%.3f,%.3f,%.3f)",
            payload.field_name, payload.page_no,
            payload.x, payload.y, payload.w, payload.h,
        )
        return {
            "id":            hint.id,
            "field_name":    hint.field_name,
            "page_no":       hint.page_no,
            "saved_as_hint": True,
            "rule_created":  False,
            "read_text":     "",
            "error":         "No text could be read from the selected region. "
                             "Region coordinates saved — try selecting a different area.",
        }

    # ── 3a. Persist value into correction record ──────────────────────────────
    rule_created_now = False
    if read_text and payload.apply_as_value:
        correction = cs.get_or_create_correction(db, row)
        old_val = cs.effective_value(row, correction, payload.field_name)
        old_str = str(old_val).strip() if old_val is not None else ""

        if old_str != read_text:
            setattr(correction, payload.field_name, read_text)
            db.add(InvoiceRowFieldAudit(
                batch_id=batch.id,
                row_id=row.id,
                field_name=payload.field_name,
                old_value=old_str or None,
                new_value=read_text,
                action="remap",
                note="Applied via region remap",
                rule_created=False,
                user_id=user.id,
                username=getattr(user, "email", None) or str(user.id),
            ))
            logger.debug(
                "save_remap: persisted %r=%r (was %r) for row %d",
                payload.field_name, read_text, old_str, row.id,
            )
        else:
            logger.debug(
                "save_remap: %r already has value %r — skipping correction write",
                payload.field_name, read_text,
            )

        # ── 3b. Upsert supplier-scoped CorrectionRule ─────────────────────────
        # rule_type="remap_field_value" lets _apply_saved_rules replay this on
        # future invoices from the same supplier without re-remapping.
        # source_pattern = normalised supplier name.
        if row.supplier_name:
            _norm = re.sub(
                r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
                "", row.supplier_name.lower(),
            )
            _norm = re.sub(r"[^a-z0-9 ]", " ", _norm)
            _norm = re.sub(r"\s+", " ", _norm).strip()

            if _norm:
                existing_rule = db.execute(
                    select(CorrectionRule).where(
                        CorrectionRule.tenant_id == batch.tenant_id,
                        CorrectionRule.rule_type == "remap_field_value",
                        CorrectionRule.field_name == payload.field_name,
                        CorrectionRule.source_pattern == _norm,
                        CorrectionRule.target_value == read_text,
                    ).limit(1)
                ).scalar_one_or_none()

                if existing_rule:
                    if not existing_rule.active:
                        existing_rule.active = True
                        existing_rule.disabled_by = None
                        existing_rule.disabled_at = None
                    existing_rule.origin_batch_id = batch.id
                    existing_rule.origin_row_id   = row.id
                    rule_created_now = True
                    logger.debug(
                        "save_remap: refreshed existing rule id=%d supplier=%r field=%r",
                        existing_rule.id, row.supplier_name, payload.field_name,
                    )
                else:
                    db.add(CorrectionRule(
                        tenant_id=batch.tenant_id,
                        company_id=batch.company_id,
                        rule_type="remap_field_value",
                        field_name=payload.field_name,
                        source_pattern=_norm,
                        target_value=read_text,
                        created_by=user.id,
                        origin_batch_id=batch.id,
                        origin_row_id=row.id,
                    ))
                    rule_created_now = True
                    logger.debug(
                        "save_remap: created remap_field_value rule "
                        "supplier=%r field=%r value=%r",
                        row.supplier_name, payload.field_name, read_text,
                    )

    db.commit()
    logger.debug(
        "save_remap: committed — hint_id=%d rule_created=%s read_text=%r",
        hint.id, rule_created_now, read_text[:40] if read_text else "",
    )

    return {
        "id":            hint.id,
        "field_name":    hint.field_name,
        "page_no":       hint.page_no,
        "saved_as_hint": True,
        "rule_created":  rule_created_now,
        "read_text":     read_text,
    }


# ── Rules management (admin + tenant-scoped user access) ──────────────────────

def _rule_to_dict(r: CorrectionRule) -> dict:
    return {
        "id": r.id,
        "tenant_id": str(r.tenant_id),
        "company_id": str(r.company_id) if r.company_id else None,
        "rule_type": r.rule_type,
        "field_name": r.field_name,
        "source_pattern": r.source_pattern,
        "target_value": r.target_value,
        "active": r.active,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "disabled_at": r.disabled_at.isoformat() if r.disabled_at else None,
        "origin_batch_id": str(r.origin_batch_id) if r.origin_batch_id else None,
    }


def _get_rule_for_user(rule_id: int, db: Session, user: M.User) -> CorrectionRule:
    r = db.get(CorrectionRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    if getattr(user, "role", None) == "admin":
        return r
    from app.db.models import UserTenant as _UT
    link = db.execute(
        select(_UT).where(_UT.user_id == user.id, _UT.tenant_id == r.tenant_id).limit(1)
    ).scalar_one_or_none()
    if not link:
        raise HTTPException(403, "Not authorised to manage this rule")
    return r


class RuleUpdatePayload(BaseModel):
    source_pattern: str | None = None
    target_value: str | None = None
    active: bool | None = None


@router.get("/rules")
def list_rules_tenant(
    company_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """List correction rules for the calling user's tenants."""
    q = select(CorrectionRule)
    if getattr(user, "role", None) != "admin":
        from app.db.models import UserTenant as _UT
        tenant_ids = [
            row[0] for row in db.execute(select(_UT.tenant_id).where(_UT.user_id == user.id)).all()
        ]
        if not tenant_ids:
            return []
        q = q.where(CorrectionRule.tenant_id.in_(tenant_ids))
    if company_id:
        from uuid import UUID as _UUID
        try:
            cid = _UUID(company_id)
            q = q.where(
                (CorrectionRule.company_id == cid) | (CorrectionRule.company_id.is_(None))
            )
        except ValueError:
            pass
    if active_only:
        q = q.where(CorrectionRule.active.is_(True))
    rules = db.execute(q.order_by(desc(CorrectionRule.created_at))).scalars().all()
    return [_rule_to_dict(r) for r in rules]


@router.get("/admin/rules")
def list_rules(db: Session = Depends(get_db), user=Depends(current_user)):
    _require_admin(user)
    rules = db.execute(
        select(CorrectionRule).order_by(desc(CorrectionRule.created_at))
    ).scalars().all()
    return [_rule_to_dict(r) for r in rules]


@router.patch("/rules/{rule_id}")
def update_rule(
    rule_id: int,
    payload: RuleUpdatePayload,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    r = _get_rule_for_user(rule_id, db, user)
    import re as _re
    new_src = _re.sub(r"\s+", " ", (payload.source_pattern or r.source_pattern).strip().lower())
    new_tgt = (payload.target_value or r.target_value or "").strip()
    if not new_src:
        raise HTTPException(422, "source_pattern cannot be blank")
    if not new_tgt:
        raise HTTPException(422, "target_value cannot be blank")
    if new_src == new_tgt.lower():
        raise HTTPException(422, "source_pattern and target_value are identical — rule would have no effect")
    existing = db.execute(
        select(CorrectionRule).where(
            CorrectionRule.tenant_id == r.tenant_id,
            CorrectionRule.rule_type == r.rule_type,
            CorrectionRule.field_name == r.field_name,
            CorrectionRule.source_pattern == new_src,
            CorrectionRule.target_value == new_tgt,
            CorrectionRule.id != rule_id,
        ).limit(1)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"An equivalent rule already exists (id={existing.id})")
    r.source_pattern = new_src
    r.target_value = new_tgt
    if payload.active is not None:
        r.active = payload.active
        if not payload.active:
            r.disabled_by = user.id
            r.disabled_at = datetime.utcnow()
        else:
            r.disabled_by = None
            r.disabled_at = None
    db.commit()
    return _rule_to_dict(r)


@router.post("/rules/{rule_id}/enable")
def enable_rule(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = _get_rule_for_user(rule_id, db, user)
    r.active = True
    r.disabled_by = None
    r.disabled_at = None
    db.commit()
    return _rule_to_dict(r)


@router.post("/rules/{rule_id}/disable")
def disable_rule_user(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = _get_rule_for_user(rule_id, db, user)
    r.active = False
    r.disabled_by = user.id
    r.disabled_at = datetime.utcnow()
    db.commit()
    return _rule_to_dict(r)


@router.delete("/rules/{rule_id}")
def delete_rule_user(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = _get_rule_for_user(rule_id, db, user)
    db.delete(r)
    db.commit()
    return {"ok": True, "deleted_id": rule_id}


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

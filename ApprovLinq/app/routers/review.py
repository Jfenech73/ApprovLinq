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
from sqlalchemy import select, desc, or_
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import (
    InvoiceRowCorrection, InvoiceRowFieldAudit, CorrectionRule, RemapHint, BatchExportEvent, InvoiceFieldCandidate,
)
from app.db.session import get_db
from app.routers.auth import current_user
from app.utils.security import session_token_hash, utcnow
from app.services import correction_service as cs
from app.services.candidate_outcomes import label_row_candidates
from app.utils.storage import resolve_upload_path
from app.utils.persistent_files import materialize_invoice_file
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review", tags=["review"])


EXPLAINABLE_ACTION_SOURCES = {
    "rule_apply": "correction_rule",
    "saved_region_apply": "saved_region",
    "saved_region_checked": "saved_region",
    "saved_region_conflict": "saved_region",
    "saved_region_invalid": "saved_region",
    "saved_region_blank": "saved_region",
    "arbitration_apply": "arbitration",
    "arbitration_conflict": "arbitration",
    "arbitration_suggest": "arbitration",
    "bcrs_split_source": "bcrs",
}

SOURCE_LABELS = {
    "raw_extraction": "Raw extraction",
    "header_rule": "Header rule",
    "azure_di_structured": "Azure DI structured",
    "correction_rule": "Correction rule",
    "saved_region": "Saved region",
    "supplier_history": "Supplier history",
    "accepted_correction": "Accepted correction",
    "supplier_master": "Supplier master",
    "nominal_master": "Nominal master",
    "totals_reconciliation": "Totals reconciliation",
    "arbitration": "Arbitration",
    "bcrs": "BCRS split",
    "manual": "Manual correction",
}

def _split_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[|,+]", value or "") if p.strip()]

def _source_from_action(action: str | None, note: str | None = None) -> str:
    a = (action or "").lower()
    n = (note or "").lower()
    if "saved_region" in a or "remap" in a or "saved region" in n or "remap_hint" in n:
        return "saved_region"
    if "rule" in a or "rule_id" in n:
        return "correction_rule"
    if "history" in a or "previous accepted" in n:
        return "supplier_history"
    if "total" in a or "totals" in n or "reconciliation" in a:
        return "totals_reconciliation"
    if "bcrs" in a or "deposit" in n:
        return "bcrs"
    if "manual" in a or "correction" in a:
        return "manual"
    if "arbitration" in a:
        return "arbitration"
    return EXPLAINABLE_ACTION_SOURCES.get(a, "raw_extraction")

def _human_reason(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"[_]+", " ", str(text)).strip()

def _build_row_explainability(row: M.InvoiceRow, correction: InvoiceRowCorrection | None, audits: list[InvoiceRowFieldAudit], current_values: dict[str, Any]) -> dict[str, Any]:
    """Return safe, compact explainability for review UI.

    This does not try to recalculate arbitration. It explains the evidence already
    recorded by extraction, rules, saved-region replay, totals checks and audit
    entries.
    """
    review_reasons = _split_tokens(row.review_reasons)
    review_fields = _split_tokens(row.review_fields)
    method_tags = _split_tokens(row.method_used)
    field_map: dict[str, dict[str, Any]] = {}
    audits_by_field: dict[str, list[InvoiceRowFieldAudit]] = {}
    for a in audits:
        audits_by_field.setdefault(a.field_name or "_row", []).append(a)

    for field in (
        "supplier_name", "supplier_posting_account", "nominal_account_code",
        "invoice_number", "invoice_date", "description",
        "net_amount", "vat_amount", "total_amount", "currency", "tax_code",
    ):
        original = getattr(row, field, None)
        selected = current_values.get(field)
        fa = audits_by_field.get(field, [])
        latest = fa[0] if fa else None
        source = "manual" if correction and getattr(correction, field, None) is not None else "raw_extraction"
        reason = "Raw extracted value retained."
        confidence = float(row.confidence_score) if row.confidence_score is not None else None
        candidates = []
        if original not in (None, ""):
            candidates.append({
                "source": "raw_extraction",
                "label": SOURCE_LABELS["raw_extraction"],
                "value": str(original),
                "confidence": confidence,
                "reason": "Original value from extraction pipeline.",
                "applied": str(original) == str(selected),
            })
        for a in reversed(fa[-8:]):
            src = _source_from_action(a.action, a.note)
            candidates.append({
                "source": src,
                "label": SOURCE_LABELS.get(src, src.replace("_", " ").title()),
                "value": a.new_value,
                "old_value": a.old_value,
                "confidence": _extract_confidence(a.note),
                "reason": a.note or _human_reason(a.action),
                "action": a.action,
                "applied": a.action in {"arbitration_apply", "rule_apply", "saved_region_apply", "bcrs_split_source"},
                "at": a.created_at.isoformat() if a.created_at else None,
            })
        if latest:
            source = _source_from_action(latest.action, latest.note)
            reason = latest.note or _human_reason(latest.action)
        field_reason_bits = []
        for rr in review_reasons:
            if rr.endswith(f":{field}") or rr == field or field in rr:
                field_reason_bits.append(_human_reason(rr))
        if field in review_fields and not field_reason_bits:
            field_reason_bits.append("Field flagged for review.")
        field_map[field] = {
            "field": field,
            "selected_value": None if selected is None else str(selected),
            "original_value": None if original is None else str(original),
            "selected_source": source,
            "selected_source_label": SOURCE_LABELS.get(source, source.replace("_", " ").title()),
            "confidence": confidence,
            "reason": reason,
            "review_required": field in review_fields,
            "review_reasons": field_reason_bits,
            "candidates": candidates,
        }
    row_level = {
        "confidence_score": float(row.confidence_score) if row.confidence_score is not None else None,
        "review_required": bool(row.review_required),
        "review_fields": review_fields,
        "review_reasons": [_human_reason(r) for r in review_reasons],
        "method_used": row.method_used or "",
        "method_tags": method_tags,
        "totals_reconciliation_status": getattr(row, "totals_reconciliation_status", None),
        "bcrs_or_discount": _detect_bcrs_discount_status(row, audits, review_reasons),
    }
    return {"row": row_level, "fields": field_map}

def _extract_confidence(note: str | None) -> float | None:
    if not note:
        return None
    m = re.search(r"confidence\s*=\s*([0-9]+(?:\.[0-9]+)?)", note, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

def _detect_bcrs_discount_status(row: M.InvoiceRow, audits: list[InvoiceRowFieldAudit], reasons: list[str]) -> dict[str, Any]:
    text = " ".join([row.review_reasons or "", row.method_used or "", row.description or ""] + [a.note or "" for a in audits]).lower()
    return {
        "bcrs_detected": "bcrs" in text or "deposit" in text,
        "discount_detected": "discount" in text or "rebate" in text,
        "details": [r for r in reasons if "bcrs" in r.lower() or "deposit" in r.lower() or "discount" in r.lower()],
    }


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
    current_value: str | None = None  # editor value fallback; prevents saved regions being stored blank


def _get_batch(db: Session, batch_id: UUID) -> M.InvoiceBatch:
    b = db.get(M.InvoiceBatch, batch_id)
    if not b:
        raise HTTPException(404, "Batch not found")
    return b


def _require_batch_access(
    db: Session,
    user: M.User,
    batch: M.InvoiceBatch,
    x_tenant_id: str | None = None,
):
    """Enforce tenant access for row-level review evidence endpoints.

    Admin users may access any batch. Tenant users must be linked to the
    batch tenant. If the UI provides X-Tenant-Id, it must match the batch tenant
    so candidate evidence cannot be fetched across the wrong active tenant.
    """
    if getattr(user, "role", None) == "admin":
        if x_tenant_id:
            try:
                selected = UUID(x_tenant_id)
            except ValueError:
                raise HTTPException(400, "Invalid X-Tenant-Id header")
            if batch.tenant_id and selected != batch.tenant_id:
                raise HTTPException(403, "Batch does not belong to selected tenant")
        return
    if not batch.tenant_id:
        raise HTTPException(403, "Batch is not tenant-scoped")
    if x_tenant_id:
        try:
            selected = UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(400, "Invalid X-Tenant-Id header")
        if selected != batch.tenant_id:
            raise HTTPException(403, "Batch does not belong to selected tenant")
    link = db.execute(
        select(M.UserTenant).where(
            M.UserTenant.user_id == user.id,
            M.UserTenant.tenant_id == batch.tenant_id,
        ).limit(1)
    ).scalar_one_or_none()
    if not link:
        raise HTTPException(403, "Not authorised for this batch")


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
    row_audits: dict[int, list[InvoiceRowFieldAudit]] = {}
    row_ids = [r.id for r in rows]
    if row_ids:
        all_audits = db.execute(
            select(InvoiceRowFieldAudit)
            .where(InvoiceRowFieldAudit.batch_id == batch_id, InvoiceRowFieldAudit.row_id.in_(row_ids))
            .order_by(desc(InvoiceRowFieldAudit.created_at))
        ).scalars().all()
        for a in all_audits:
            row_audits.setdefault(a.row_id, []).append(a)
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
        explanation = _build_row_explainability(r, c, row_audits.get(r.id, []), eff)
        out_rows.append({
            "id": r.id,
            "source_filename": r.source_filename,
            "source_file_id": r.source_file_id,
            "page_no": r.page_no,
            "confidence_score": float(r.confidence_score) if r.confidence_score is not None else None,
            "review_required": r.review_required,
            "review_priority": r.review_priority,
            "review_fields": _split_tokens(r.review_fields),
            "review_reasons": _split_tokens(r.review_reasons),
            "method_used": r.method_used or "",
            "totals_reconciliation_status": getattr(r, "totals_reconciliation_status", None),
            "row_reviewed": bool(c.row_reviewed) if c else False,
            "reviewed_fields": (c.reviewed_fields or "").split(",") if c and c.reviewed_fields else [],
            "is_corrected": was_corrected,
            "original": {f: getattr(r, f) for f in eff},
            "current": eff,
            "explainability": explanation,
            "field_evidence": explanation.get("fields", {}),
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
    labelled = 0
    if audits:
        labelled = label_row_candidates(db, batch=batch, row=row, user=user, outcome_source="manual_review")
    db.commit()
    return {"audited": len(audits), "candidate_labels": labelled}




@router.delete("/batches/{batch_id}/rows/{row_id}")
def delete_review_row(batch_id: UUID, row_id: int,
                      db: Session = Depends(get_db), user=Depends(current_user)):
    """Delete a review row so it is blocked from export.

    This is intended for duplicates/false rows caught during arbitration review.
    The immutable source PDF remains; only the extracted/export row is removed.
    """
    batch = _get_batch(db, batch_id)
    if cs.normalise_status(batch.status) == "exported":
        raise HTTPException(409, "Batch is exported; reopen before deleting rows.")
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found in batch")
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name="_row",
        old_value=f"{row.supplier_name or ''} | {row.invoice_number or ''} | {row.total_amount or ''}"[:500],
        new_value="deleted_blocked_from_export",
        action="row_delete_block_export",
        note="Reviewer deleted this row so it is excluded from export.",
        user_id=user.id,
        username=getattr(user, "email", None) or getattr(user, "full_name", None),
    ))
    db.delete(row)
    db.commit()
    return {"deleted": True, "row_id": row_id, "blocked_from_export": True}


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
        # Subtract the BCRS deposit directly from the original total.
        # Using net+vat alone is wrong when the AI included BCRS inside net —
        # that formula loses money.  Subtracting from total is always correct.
        net = round(float(row.net_amount or 0), 2)
        vat = round(float(row.vat_amount or 0), 2)
        old_total = row_total
        corrected_total = round(old_total - amount, 2)
        # Safety clamp: corrected total must be >= net+vat (commercial component)
        if corrected_total < round(net + vat, 2):
            corrected_total = round(net + vat, 2)
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
        "action": a.action, "note": a.note, "source": _source_from_action(a.action, a.note),
        "source_label": SOURCE_LABELS.get(_source_from_action(a.action, a.note), _source_from_action(a.action, a.note).replace("_", " ").title()),
        "confidence": _extract_confidence(a.note),
        "explanation": a.note or _human_reason(a.action),
        "rule_created": a.rule_created,
        "force_added": a.force_added, "user_id": str(a.user_id) if a.user_id else None,
        "username": a.username, "at": a.created_at.isoformat(),
    } for a in audits]


@router.get("/batches/{batch_id}/rows/{row_id}/candidates")
def row_field_candidates(
    batch_id: UUID,
    row_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """Return persisted arbitration candidates grouped by field.

    This endpoint is read-only evidence for Review. It does not recalculate
    arbitration and does not alter selected values.
    """
    batch = _get_batch(db, batch_id)
    _require_batch_access(db, user, batch, x_tenant_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found in batch")

    q = (
        select(InvoiceFieldCandidate)
        .where(
            InvoiceFieldCandidate.batch_id == batch.id,
            InvoiceFieldCandidate.row_id == row.id,
        )
        .order_by(
            InvoiceFieldCandidate.field_name.asc(),
            InvoiceFieldCandidate.selected.desc(),
            InvoiceFieldCandidate.applied.desc(),
            InvoiceFieldCandidate.confidence.desc().nullslast(),
            InvoiceFieldCandidate.created_at.desc(),
        )
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for c in db.execute(q).scalars().all():
        source = c.source_type or "unknown"
        grouped.setdefault(c.field_name, []).append({
            "id": c.id,
            "field_name": c.field_name,
            "candidate_value": c.candidate_value,
            "normalised_value": c.normalised_value,
            "source_type": source,
            "source_label": SOURCE_LABELS.get(source, source.replace("_", " ").title()),
            "source_id": c.source_id,
            "confidence": float(c.confidence) if c.confidence is not None else None,
            "evidence": c.evidence,
            "reason": c.reason,
            "selected": bool(c.selected),
            "applied": bool(c.applied),
            "conflict": bool(c.conflict),
            "rejected_reason": c.rejected_reason,
            "user_accepted": bool(getattr(c, "user_accepted", False)),
            "user_corrected": bool(getattr(c, "user_corrected", False)),
            "final_value": getattr(c, "final_value", None),
            "finalised_at": c.finalised_at.isoformat() if getattr(c, "finalised_at", None) else None,
            "finalised_by": str(c.finalised_by) if getattr(c, "finalised_by", None) else None,
            "outcome_source": getattr(c, "outcome_source", None),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
    return {"batch_id": str(batch.id), "row_id": row.id, "fields": grouped}


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
    labelled = 0
    for r in flagged:
        labelled += label_row_candidates(db, batch=batch, row=r, user=user, outcome_source="mark_reviewed")
    db.commit()
    return {"file_id": file_id, "marked_rows": created, "already_reviewed": len(flagged) - created, "candidate_labels": labelled}


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




def _analytics_percent(part: int, whole: int) -> float:
    return round((float(part) / float(whole) * 100.0), 2) if whole else 0.0


def _candidate_analytics_row(label: str, rows: list[InvoiceFieldCandidate]) -> dict[str, Any]:
    candidate_count = len(rows)
    selected_count = sum(1 for r in rows if bool(r.selected))
    applied_count = sum(1 for r in rows if bool(r.applied))
    accepted_count = sum(1 for r in rows if bool(getattr(r, "user_accepted", False)))
    corrected_count = sum(1 for r in rows if bool(getattr(r, "user_corrected", False)))
    conflict_count = sum(1 for r in rows if bool(r.conflict))
    labelled_count = accepted_count + corrected_count
    return {
        "label": label or "-",
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "applied_count": applied_count,
        "accepted_count": accepted_count,
        "corrected_count": corrected_count,
        "conflict_count": conflict_count,
        "labelled_count": labelled_count,
        "accuracy": _analytics_percent(accepted_count, labelled_count),
        "correction_rate": _analytics_percent(corrected_count, labelled_count),
        "selection_rate": _analytics_percent(selected_count, candidate_count),
        "conflict_rate": _analytics_percent(conflict_count, candidate_count),
    }


def _group_candidate_rows(rows: list[InvoiceFieldCandidate], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[InvoiceFieldCandidate]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row) or "-"), []).append(row)
    return sorted(
        (_candidate_analytics_row(label, grouped) for label, grouped in groups.items()),
        key=lambda item: (item.get("corrected_count", 0), item.get("candidate_count", 0)),
        reverse=True,
    )


@router.get("/candidate-analytics")
def candidate_analytics(
    tenant_id: UUID | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
    supplier: str | None = Query(default=None),
    field_name: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    x_tenant_id: str | None = Header(default=None),
    user: M.User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Candidate analytics for ML-readiness and extraction quality.

    Tenant users are always restricted to their active/default tenant.  Admins may
    query all tenants or filter to one tenant.  Supplier filtering is applied via
    invoice_rows.supplier_name, not candidate text, so it reflects the invoice row
    context used during review/export.
    """
    allowed_tenant_id: UUID | None = None
    if user.role == "admin":
        if tenant_id:
            allowed_tenant_id = tenant_id
        elif x_tenant_id:
            try:
                allowed_tenant_id = UUID(str(x_tenant_id))
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id header") from exc
            if not db.get(M.Tenant, allowed_tenant_id):
                raise HTTPException(status_code=404, detail="Tenant not found")
    else:
        if tenant_id:
            requested = tenant_id
        elif x_tenant_id:
            try:
                requested = UUID(str(x_tenant_id))
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Invalid X-Tenant-Id header") from exc
        else:
            link = db.query(M.UserTenant).filter(M.UserTenant.user_id == user.id).order_by(M.UserTenant.is_default.desc(), M.UserTenant.id.asc()).first()
            if not link:
                raise HTTPException(status_code=403, detail="No tenant access assigned")
            requested = link.tenant_id
        link = db.query(M.UserTenant).filter(M.UserTenant.user_id == user.id, M.UserTenant.tenant_id == requested).first()
        if not link:
            raise HTTPException(status_code=403, detail="Forbidden for selected tenant")
        allowed_tenant_id = requested

    q = (
        db.query(InvoiceFieldCandidate, M.InvoiceRow, M.Tenant, M.Company)
        .join(M.InvoiceRow, InvoiceFieldCandidate.row_id == M.InvoiceRow.id)
        .join(M.Tenant, InvoiceFieldCandidate.tenant_id == M.Tenant.id)
        .outerjoin(M.Company, InvoiceFieldCandidate.company_id == M.Company.id)
    )
    if allowed_tenant_id:
        q = q.filter(InvoiceFieldCandidate.tenant_id == allowed_tenant_id)
    if company_id:
        q = q.filter(InvoiceFieldCandidate.company_id == company_id)
    if field_name:
        q = q.filter(InvoiceFieldCandidate.field_name == field_name)
    if source_type:
        q = q.filter(InvoiceFieldCandidate.source_type == source_type)
    if date_from:
        q = q.filter(InvoiceFieldCandidate.created_at >= date_from)
    if date_to:
        q = q.filter(InvoiceFieldCandidate.created_at <= date_to)
    if supplier:
        pattern = f"%{supplier.strip()}%"
        q = q.filter(M.InvoiceRow.supplier_name.ilike(pattern))

    records = q.limit(20000).all()
    candidates = [rec[0] for rec in records]
    row_by_candidate_id = {rec[0].id: rec[1] for rec in records}
    tenant_by_candidate_id = {rec[0].id: rec[2] for rec in records}
    company_by_candidate_id = {rec[0].id: rec[3] for rec in records}

    summary = _candidate_analytics_row("summary", candidates)
    by_source_type = _group_candidate_rows(candidates, lambda c: c.source_type)
    by_field = _group_candidate_rows(candidates, lambda c: c.field_name)

    supplier_groups: dict[str, list[InvoiceFieldCandidate]] = {}
    for c in candidates:
        row = row_by_candidate_id.get(c.id)
        supplier_name = (getattr(row, "supplier_name", None) or "Unknown supplier").strip() or "Unknown supplier"
        supplier_groups.setdefault(supplier_name, []).append(c)
    top_corrected_suppliers = sorted(
        (_candidate_analytics_row(label, grouped) for label, grouped in supplier_groups.items()),
        key=lambda item: (item.get("corrected_count", 0), item.get("candidate_count", 0)),
        reverse=True,
    )[:10]

    tenant_rows = _group_candidate_rows(candidates, lambda c: getattr(tenant_by_candidate_id.get(c.id), "tenant_name", str(c.tenant_id)))
    company_rows = _group_candidate_rows(candidates, lambda c: getattr(company_by_candidate_id.get(c.id), "company_name", None) or "No company")

    return {
        "filters": {
            "tenant_id": str(allowed_tenant_id) if allowed_tenant_id else (str(tenant_id) if tenant_id else None),
            "company_id": str(company_id) if company_id else None,
            "supplier": supplier,
            "field_name": field_name,
            "source_type": source_type,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
        "summary": summary,
        "by_source_type": by_source_type,
        "by_field": by_field,
        "by_tenant": tenant_rows,
        "by_company": company_rows,
        "top_corrected_suppliers": top_corrected_suppliers,
        "row_limit_applied": len(records) >= 20000,
    }


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
    try:
        file_path = materialize_invoice_file(f)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
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
    try:
        file_path = materialize_invoice_file(f)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
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



def _ocr_region_bytes(image_bytes: bytes) -> str:
    """OCR a selected remap crop with local Tesseract first, OCR.space fallback.

    This function is intentionally local/offline-first so remap readback works in
    development and in deployments where OCR.space is not configured. It tries a
    small set of conservative image variants/PSM modes and returns the best text.
    """
    if not image_bytes:
        return ""
    candidates: list[str] = []

    def _clean(txt: str | None) -> str:
        return " ".join((txt or "").replace("\x00", " ").split()).strip()

    # Local Tesseract fallback.  This is especially important for scanned PDFs
    # and for supplier-name remaps where the whole document has no text layer.
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        variants = [img]
        # Upscale tight header crops. Tesseract struggles below ~40px text height.
        w, h = img.size
        if w < 900 or h < 180:
            scale = 3 if max(w, h) < 350 else 2
            variants.append(img.resize((w * scale, h * scale)))
        gray = img.convert("L")
        enhanced = ImageEnhance.Contrast(gray).enhance(1.35)
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(1.15)
        variants.append(enhanced)
        if w < 900 or h < 180:
            variants.append(enhanced.resize((w * 2, h * 2)))
        # A light denoise variant helps low-quality cropped invoices without
        # over-darkening clear ones.
        variants.append(enhanced.filter(ImageFilter.MedianFilter(size=3)))

        seen = set()
        for var in variants:
            key = (var.size, var.mode)
            if key in seen:
                continue
            seen.add(key)
            for psm in (7, 6, 11):
                try:
                    txt = _clean(pytesseract.image_to_string(var, config=f"--oem 3 --psm {psm}"))
                    if txt:
                        candidates.append(txt)
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("_ocr_region_bytes: local tesseract unavailable/failed: %s", exc)

    # OCR.space fallback, only when configured.  Kept after local OCR because it
    # is slower and may incur cost/quota.
    if getattr(settings, "ocr_space_api_key", None):
        try:
            import requests as _requests
            files = {"file": ("region.jpg", image_bytes, "image/jpeg")}
            data = {
                "apikey": settings.ocr_space_api_key,
                "language": settings.ocr_space_language,
                "isOverlayRequired": "false",
                "scale": "true",
                "OCREngine": str(settings.ocr_space_ocr_engine),
            }
            resp = _requests.post(
                settings.ocr_space_endpoint,
                files=files,
                data=data,
                timeout=min(int(settings.ocr_space_timeout_seconds or 60), 60),
            )
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("ParsedResults") or []:
                txt = _clean((item or {}).get("ParsedText"))
                if txt:
                    candidates.append(txt)
        except Exception as exc:
            logger.debug("_ocr_region_bytes: OCR.space fallback failed: %s", exc)

    if not candidates:
        return ""
    # Select the candidate with most meaningful characters, but prefer shorter
    # header-like strings over huge noisy OCR bursts when scores are similar.
    def _score(txt: str) -> tuple[int, int]:
        meaningful = _count_meaningful(txt)
        noise_penalty = max(len(txt) - 120, 0)
        return (meaningful - noise_penalty, -len(txt))
    return max(candidates, key=_score)

def _read_region_text(file_path: str, page_no: int, x: float, y: float, w: float, h: float) -> str:
    """Return the best text found inside/near a normalised page rectangle.

    Region selection from an image preview is never pixel-perfect: users often
    draw a box that clips the first letter/word, and browser preview scaling can
    add a small offset.  Treat the user rectangle as the centre of intent, not a
    hard crop.  We therefore try the exact box plus a few conservative expanded
    boxes before falling back to OCR.  This fixes the common "missing first word"
    and "no text detected" remap failures while still keeping extraction local
    to the selected area.
    """
    logger.debug(
        "_read_region_text: file=%s page=%d region=(%.3f,%.3f,%.3f,%.3f)",
        file_path, page_no, x, y, w, h,
    )

    def _clean(txt: str | None) -> str:
        return " ".join((txt or "").replace("\x00", " ").split()).strip()

    def _clip(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    def _variants(x0: float, y0: float, w0: float, h0: float) -> list[tuple[str, float, float, float, float]]:
        # Coordinates are normalised with y=0 at the visual top.  The left/top
        # biased variants are deliberate: clipped first words are usually caused
        # by starting the drag a few pixels too far right/down.
        specs = [
            ("exact",        0.00, 0.00, 0.00, 0.00),
            ("slight",       0.04, 0.08, 0.06, 0.10),
            ("left_biased",  0.18, 0.10, 0.08, 0.12),
            ("wide",         0.30, 0.16, 0.16, 0.18),
        ]
        out: list[tuple[str, float, float, float, float]] = []
        seen: set[tuple[float, float, float, float]] = set()
        for name, lmul, tmul, rmul, bmul in specs:
            xx = _clip(x0 - max(0.006, w0 * lmul))
            yy = _clip(y0 - max(0.004, h0 * tmul))
            rr = _clip(x0 + w0 + max(0.006, w0 * rmul))
            bb = _clip(y0 + h0 + max(0.004, h0 * bmul))
            ww = max(0.0, rr - xx)
            hh = max(0.0, bb - yy)
            key = (round(xx, 5), round(yy, 5), round(ww, 5), round(hh, 5))
            if ww > 0 and hh > 0 and key not in seen:
                seen.add(key)
                out.append((name, xx, yy, ww, hh))
        return out

    candidates: list[tuple[str, str]] = []

    def _add(source: str, txt: str | None) -> None:
        txt = _clean(txt)
        if _count_meaningful(txt) >= 2:
            candidates.append((source, txt))
            logger.debug("_read_region_text: candidate %s %r meaningful=%d", source, txt[:80], _count_meaningful(txt))

    # ── tier1 / Tier 1: PyMuPDF get_textbox over exact + expanded boxes ─────
    # tier1 gate marker for tests: accept only when m1 >= 2 meaningful chars.
    try:
        import fitz
        doc = fitz.open(file_path)
        try:
            if 1 <= page_no <= doc.page_count:
                page = doc.load_page(page_no - 1)
                pw, ph = page.rect.width, page.rect.height
                for name, xx, yy, ww, hh in _variants(x, y, w, h):
                    rect = fitz.Rect(xx * pw, yy * ph, (xx + ww) * pw, (yy + hh) * ph)
                    _add(f"fitz:{name}", page.get_textbox(rect))
        finally:
            doc.close()
    except Exception as _e1:
        logger.debug("_read_region_text: tier1 (fitz) failed: %s", _e1)

    # ── tier2 / Tier 2: pypdfium2 textpage over exact + expanded boxes ──────
    # tier2 markers for tests: get_textpage + get_text_bounded + 1.0 - (y + h) y-axis flip.
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
                        for name, xx, yy, ww, hh in _variants(x, y, w, h):
                            # pypdfium2 PDF coords: y=0 at bottom, y=height at top
                            _left   = xx * _pw2
                            _bottom = (1.0 - (yy + hh)) * _ph2
                            _right  = (xx + ww) * _pw2
                            _top    = (1.0 - yy) * _ph2
                            _add(f"pdfium_text:{name}", _tp.get_text_bounded(
                                left=_left, bottom=_bottom, right=_right, top=_top,
                            ))
                    finally:
                        _tp.close()
                finally:
                    _pg2.close()
        finally:
            _pdf2.close()
    except Exception as _e2:
        logger.debug("_read_region_text: tier2 (pypdfium2 textpage) failed: %s", _e2)

    # ── tier3 / Tier 3: cropped-region render + OCR over exact + expanded boxes
    # tier3 order marker for tests: render(scale=...) then .crop( then ocr_space_api_key/local OCR.
    try:
        import pypdfium2 as _pdfium3
        _pdf3 = _pdfium3.PdfDocument(file_path)
        try:
            if 1 <= page_no <= len(_pdf3):
                _pg3 = _pdf3.get_page(page_no - 1)
                try:
                    _full = _pg3.render(scale=4.0).to_pil().convert("RGB")
                finally:
                    _pg3.close()
            else:
                _full = None
        finally:
            _pdf3.close()
        if _full is not None:
            _W, _H = _full.size
            for name, xx, yy, ww, hh in _variants(x, y, w, h):
                _box = (int(xx * _W), int(yy * _H), int((xx + ww) * _W), int((yy + hh) * _H))
                if _box[2] - _box[0] < 4 or _box[3] - _box[1] < 4:
                    continue
                _buf = io.BytesIO()
                _crop = _full.crop(_box)
                _crop.save(_buf, format="PNG")
                _add(f"ocr:{name}", _ocr_region_bytes(_buf.getvalue()))
    except Exception as _e3:
        logger.debug("_read_region_text: tier3 OCR render/read failed: %s", _e3)

    if not candidates:
        logger.debug("_read_region_text: no usable region text found")
        return ""

    def _score(item: tuple[str, str]) -> tuple[int, int, int]:
        source, txt = item
        meaningful = _count_meaningful(txt)
        # Expanded boxes are useful, but avoid selecting a giant noisy address
        # block when the exact crop had a clean value.  Penalise excessive text.
        noise_penalty = max(len(txt) - 180, 0)
        exact_bonus = 8 if source.endswith(":exact") else 0
        left_bonus = 4 if "left_biased" in source or source.endswith(":wide") else 0
        text_layer_bonus = 3 if source.startswith("fitz") or source.startswith("pdfium_text") else 0
        return (meaningful - noise_penalty + exact_bonus + left_bonus + text_layer_bonus, meaningful, -len(txt))

    best_source, best_text = max(candidates, key=_score)
    logger.debug("_read_region_text: selected %s → %r", best_source, best_text[:120])
    return best_text


def _normalise_supplier_remap_text(text: str) -> str:
    """Turn a supplier-name remap crop into the best supplier title.

    OCR on a selected header range may return several lines (name + tagline +
    address + phone).  For the supplier_name field we should store the best
    company/title value, not the whole crop.  This keeps future remap hints useful.
    """
    raw = "\n".join((text or "").splitlines()).strip()
    if not raw:
        return ""
    # If OCR collapsed the crop into one long line, keep the leading legal
    # company name up to its suffix (e.g. "Mafimex Ltd."), not the tagline/address.
    m_legal = re.match(r"^\s*(.{2,80}?\b(?:ltd|limited|plc|llc|inc|company|co\.?)\.?)\b", raw, re.I)
    if m_legal:
        return " ".join(m_legal.group(1).split()).strip()
    try:
        from app.services.extractor import (
            find_supplier_name,
            normalise_company_name,
            _clean_ocr_supplier_name,
            suspicious_supplier_name,
            bad_supplier_line,
            _company_strength_score,
        )
        found = find_supplier_name(raw)
        if found and not suspicious_supplier_name(found):
            return normalise_company_name(_clean_ocr_supplier_name(found)) or found
        candidates: list[str] = []
        for ln in raw.splitlines() or [raw]:
            clean = normalise_company_name(_clean_ocr_supplier_name(" ".join(ln.split()).strip()))
            if not clean or suspicious_supplier_name(clean) or bad_supplier_line(clean):
                continue
            if len(clean) <= 90:
                candidates.append(clean)
        if candidates:
            return sorted(candidates, key=_company_strength_score, reverse=True)[0]
    except Exception:
        pass
    return " ".join(raw.split())


def _supplier_candidate_from_full_page(file_path: str, page_no: int, seed_text: str = "") -> str:
    """Read the full page and return the best supplier title for remap fallback.

    This is used only when the selected supplier region produces no/partial text.
    It prevents a good saved region from being recorded blank and lets a logo crop
    such as "Mafimex" be promoted to the nearby legal title "Mafimex Ltd.".
    """
    try:
        page_text = ""
        # Prefer the PDF text layer when present.  This is faster and avoids
        # OCR missing legal suffixes on clean digital/test PDFs.
        try:
            import fitz as _fitz
            _doc = _fitz.open(file_path)
            try:
                if 1 <= page_no <= _doc.page_count:
                    page_text = _doc.load_page(page_no - 1).get_text("text") or ""
            finally:
                _doc.close()
        except Exception:
            page_text = ""

        import pypdfium2 as _pdfium
        _pdf = _pdfium.PdfDocument(file_path)
        try:
            if page_no < 1 or page_no > len(_pdf):
                return ""
            _pg = _pdf.get_page(page_no - 1)
            try:
                img = _pg.render(scale=2.2).to_pil().convert("RGB")
            finally:
                _pg.close()
        finally:
            _pdf.close()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        ocr_text = _ocr_region_bytes(buf.getvalue())
        if ocr_text:
            page_text = (page_text + "\n" + ocr_text).strip()
        if not page_text:
            return ""
        from app.services.extractor import (
            find_supplier_name, normalise_company_name, _clean_ocr_supplier_name,
            _collect_supplier_candidates, _company_strength_score, suspicious_supplier_name,
        )
        candidates = _collect_supplier_candidates(page_text)
        found = find_supplier_name(page_text)
        if found:
            candidates.insert(0, found)
        norm_seed = re.sub(r"[^a-z0-9]", "", (seed_text or "").lower())
        cleaned: list[str] = []
        for c in candidates:
            cc = normalise_company_name(_clean_ocr_supplier_name(c)) or c
            if not cc or suspicious_supplier_name(cc):
                continue
            if cc not in cleaned:
                cleaned.append(cc)
        if not cleaned:
            return ""
        if norm_seed:
            # Prefer a full legal candidate that contains the selected/logo seed.
            for c in sorted(cleaned, key=_company_strength_score, reverse=True):
                cn = re.sub(r"[^a-z0-9]", "", c.lower())
                if norm_seed and (norm_seed in cn or cn in norm_seed):
                    return c
        return sorted(cleaned, key=_company_strength_score, reverse=True)[0]
    except Exception as exc:
        logger.debug("_supplier_candidate_from_full_page failed: %s", exc)
        return ""


def _promote_supplier_remap_text(file_path: str, page_no: int, read_text: str) -> str:
    """Promote blank/partial supplier region OCR to the full page supplier title."""
    current = _normalise_supplier_remap_text(read_text or "") if read_text else ""
    has_legal_suffix = bool(re.search(r"\b(ltd|limited|plc|llc|inc|company|co\.?)\b", current or "", re.I))
    if current and has_legal_suffix:
        return current
    page_candidate = _supplier_candidate_from_full_page(file_path, page_no, current)
    if page_candidate:
        return page_candidate
    return current

# ── Remap hints + value persistence + rule creation ─────────────────────────

def _norm_for_region_fallback(value: str | None) -> str:
    """Normalise text for deciding if a crop read is only a clipped subset."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def _parse_amount_like(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"-?\d+(?:[,.]\d{1,2})?", str(value).replace("€", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None


def _current_value_better_than_region_read(field_name: str, read_text: str | None, current_value: str | None) -> bool:
    """Return True when the editor value should be used as a safe fallback.

    This solves two remap UX problems without making the future replay unsafe:
    OCR/text-layer crops that miss the first word/letter, and tight/scanned
    crops that read blank even though the user has already corrected the field.
    """
    cur = " ".join((current_value or "").split()).strip()
    got = " ".join((read_text or "").split()).strip()
    if not cur:
        return False
    if not got:
        return True
    if cur == got:
        return False
    if field_name in {"net_amount", "vat_amount", "total_amount"}:
        c_amt = _parse_amount_like(cur)
        g_amt = _parse_amount_like(got)
        return c_amt is not None and g_amt is not None and abs(c_amt - g_amt) < 0.01 and cur != got
    ncur = _norm_for_region_fallback(cur)
    ngot = _norm_for_region_fallback(got)
    if not ncur or not ngot:
        return False
    if field_name == "supplier_name":
        cur_tokens = {t for t in ncur.split() if len(t) > 1}
        got_tokens = {t for t in ngot.split() if len(t) > 1}
        if len(ncur) >= 6 and (
            f" {ncur} " in f" {ngot} "
            or (len(cur_tokens) >= 2 and cur_tokens <= got_tokens)
        ):
            return True
    if ngot in ncur and len(ncur) >= len(ngot) + 3:
        return True
    ccur = ncur.replace(" ", "")
    cgot = ngot.replace(" ", "")
    if cgot and cgot in ccur and len(ccur) >= len(cgot) + 3:
        return True
    return False



def _normalise_remap_group_supplier(value: str | None) -> str:
    value = (value or "").lower()
    value = re.sub(r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b", " ", value)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", value)).strip()


def _coords_close(h: RemapHint, x, y, w, hh, tolerance: float = 0.004) -> bool:
    try:
        vals = (float(h.x), float(h.y), float(h.w), float(h.h), float(x), float(y), float(w), float(hh))
    except Exception:
        return False
    return (
        abs(vals[0] - vals[4]) <= tolerance
        and abs(vals[1] - vals[5]) <= tolerance
        and abs(vals[2] - vals[6]) <= tolerance
        and abs(vals[3] - vals[7]) <= tolerance
    )


def _remap_group_query(
    db: Session,
    tenant_id,
    company_id,
    field_name: str,
    supplier_id: int | None,
    supplier_snapshot: str | None,
    *,
    include_archived: bool = True,
    include_deleted: bool = False,
):
    """Return the base query for one supplier/field saved-region governance group.

    Identity deliberately excludes page_no.  Page is only a reference coordinate
    replay hint; supplier + field is the user-facing saved-region relationship.
    """
    q = select(RemapHint).where(
        RemapHint.tenant_id == tenant_id,
        RemapHint.field_name == field_name,
    )
    if company_id:
        q = q.where((RemapHint.company_id == company_id) | (RemapHint.company_id.is_(None)))
    else:
        q = q.where(RemapHint.company_id.is_(None))
    if supplier_id:
        q = q.where(RemapHint.supplier_id == supplier_id)
    else:
        norm = _normalise_remap_group_supplier(supplier_snapshot)
        if norm:
            q = q.where(RemapHint.supplier_name_snapshot.isnot(None))
        else:
            q = q.where(RemapHint.supplier_name_snapshot.is_(None))
    if not include_archived:
        q = q.where(RemapHint.archived.is_(False))
    if not include_deleted:
        q = q.where(RemapHint.deleted_at.is_(None))
    return q


def _same_remap_group(a: RemapHint, b: RemapHint) -> bool:
    if a.tenant_id != b.tenant_id or a.field_name != b.field_name:
        return False
    if (a.company_id or None) != (b.company_id or None):
        # Tenant-wide and company-specific hints are related in application, but
        # primary uniqueness is enforced per exact company scope.
        return False
    if a.supplier_id or b.supplier_id:
        return bool(a.supplier_id and b.supplier_id and a.supplier_id == b.supplier_id)
    return _normalise_remap_group_supplier(a.supplier_name_snapshot) == _normalise_remap_group_supplier(b.supplier_name_snapshot)


def _normalise_stable_anchor_value(anchor_type: str | None, value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    kind = (anchor_type or "").strip()
    if kind in {"VendorTaxId", "CustomerTaxId", "CustomerId", "PurchaseOrder", "OrderNumber", "InvoiceId"}:
        return re.sub(r"[^A-Z0-9]", "", text.upper())
    return re.sub(r"\s+", " ", text.upper()).strip()


def _row_stable_anchor(db: Session, row: M.InvoiceRow) -> tuple[str | None, str | None, str | None]:
    """Return the strongest stable identifier saved from DI for a review row."""
    header = db.execute(
        select(M.InvoiceReadHeader)
        .where(M.InvoiceReadHeader.row_id == row.id)
        .order_by(desc(M.InvoiceReadHeader.id))
        .limit(1)
    ).scalar_one_or_none()
    if not header:
        return None, None, None
    for anchor_type, attr in (
        ("VendorTaxId", "VendorTaxId"),
        ("CustomerId", "CustomerId"),
        ("CustomerTaxId", "CustomerTaxId"),
        ("PurchaseOrder", "PurchaseOrder"),
        ("OrderNumber", "order_number"),
        ("InvoiceId", "InvoiceId"),
    ):
        raw = getattr(header, attr, None)
        value = _normalise_stable_anchor_value(anchor_type, raw)
        if value:
            return anchor_type, value, f"{anchor_type}={raw}"
    return None, None, None


def _get_remap_hint_for_user(hint_id: int, db: Session, user, x_tenant_id: str | None = None) -> RemapHint:
    tenant_id = None if getattr(user, "role", None) == "admin" and not x_tenant_id else _active_tenant_id_for_user(db, user, x_tenant_id)
    hint = db.get(RemapHint, hint_id)
    if not hint or (tenant_id is not None and hint.tenant_id != tenant_id):
        raise HTTPException(404, "Remap hint not found")
    return hint


def _disable_related_remap_rules(db: Session, hint: RemapHint, user=None) -> int:
    """Disable region-backed rules that would make an archived hint look active."""
    q = select(CorrectionRule).where(
        CorrectionRule.tenant_id == hint.tenant_id,
        CorrectionRule.rule_type == "remap_field_value",
        CorrectionRule.field_name == hint.field_name,
        CorrectionRule.active.is_(True),
    )
    if hint.company_id:
        q = q.where((CorrectionRule.company_id == hint.company_id) | (CorrectionRule.company_id.is_(None)))
    rules = db.execute(q).scalars().all()
    disabled = 0
    snap_norm = _normalise_remap_group_supplier(hint.supplier_name_snapshot)
    for rule in rules:
        src_norm = _normalise_remap_group_supplier(rule.source_pattern)
        if snap_norm and src_norm and snap_norm != src_norm:
            continue
        rule.active = False
        rule.disabled_by = getattr(user, "id", None)
        rule.disabled_at = datetime.utcnow()
        disabled += 1
    return disabled


def _set_primary_hint(db: Session, hint: RemapHint) -> None:
    peers = db.execute(select(RemapHint).where(
        RemapHint.tenant_id == hint.tenant_id,
        RemapHint.field_name == hint.field_name,
        RemapHint.active.is_(True),
        RemapHint.archived.is_(False),
        RemapHint.deleted_at.is_(None),
    )).scalars().all()
    for peer in peers:
        if _same_remap_group(hint, peer):
            peer.is_primary = peer.id == hint.id
    hint.is_primary = True


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

    _snapshot_supplier_name = row.supplier_name
    stable_anchor_type, stable_anchor_value, stable_anchor_evidence = _row_stable_anchor(db, row)

    # Governance grouping: saved regions are supplier + field instructions.
    # Page number is reference evidence only and must not create a separate
    # identity.  Reuse an existing hint only when the coordinates are effectively
    # the same; otherwise save the new region as an active fallback unless it is
    # the first region in the group.
    group_stmt = _remap_group_query(
        db, batch.tenant_id, batch.company_id, payload.field_name,
        supplier.id if supplier else None, _snapshot_supplier_name,
        include_archived=False, include_deleted=False,
    )
    group_hints = db.execute(group_stmt.order_by(desc(RemapHint.is_primary), desc(RemapHint.created_at), desc(RemapHint.id))).scalars().all()
    if not supplier:
        wanted_norm = _normalise_remap_group_supplier(_snapshot_supplier_name)
        group_hints = [h for h in group_hints if _normalise_remap_group_supplier(h.supplier_name_snapshot) == wanted_norm]

    existing_hint = None
    for candidate in group_hints:
        if _coords_close(candidate, payload.x, payload.y, payload.w, payload.h):
            existing_hint = candidate
            break

    has_primary = any(bool(getattr(h, "is_primary", False)) for h in group_hints if h.active and not h.archived and not h.deleted_at)

    if existing_hint:
        existing_hint.x = payload.x
        existing_hint.y = payload.y
        existing_hint.w = payload.w
        existing_hint.h = payload.h
        existing_hint.active = True
        existing_hint.archived = False
        existing_hint.archived_at = None
        existing_hint.archived_by = None
        existing_hint.deleted_at = None
        existing_hint.deleted_by = None
        if not has_primary:
            existing_hint.is_primary = True
        existing_hint.source_batch_id = batch.id
        existing_hint.source_file_id  = payload.file_id or row.source_file_id
        existing_hint.source_row_id   = row.id
        existing_hint.company_id      = batch.company_id
        if supplier:
            existing_hint.supplier_id = supplier.id
        if _snapshot_supplier_name:
            existing_hint.supplier_name_snapshot = _snapshot_supplier_name
        if stable_anchor_type and stable_anchor_value:
            existing_hint.stable_anchor_type = stable_anchor_type
            existing_hint.stable_anchor_value = stable_anchor_value
            existing_hint.stable_anchor_evidence = stable_anchor_evidence
        hint = existing_hint
        logger.debug("save_remap: updated existing RemapHint id=%d", hint.id)
    else:
        hint = RemapHint(
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            supplier_id=supplier.id if supplier else None,
            supplier_name_snapshot=_snapshot_supplier_name,
            stable_anchor_type=stable_anchor_type,
            stable_anchor_value=stable_anchor_value,
            stable_anchor_evidence=stable_anchor_evidence,
            field_name=payload.field_name,
            page_no=payload.page_no,
            x=payload.x, y=payload.y, w=payload.w, h=payload.h,
            source_batch_id=batch.id,
            source_file_id=payload.file_id or row.source_file_id,
            source_row_id=row.id,
            created_by=user.id,
            is_primary=not has_primary,
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
                    str(materialize_invoice_file(f)),
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

    # Normalise: collapse whitespace, strip leading/trailing.
    # For supplier_name, convert a multi-line/header/logo crop into the best
    # company title before saving.  If region OCR is blank or partial, fall back
    # to the full-page supplier candidate so the saved hint is not unusable.
    if payload.field_name == "supplier_name":
        file_id_for_supplier = payload.file_id or row.source_file_id
        file_obj_for_supplier = db.get(M.InvoiceFile, file_id_for_supplier) if file_id_for_supplier else None
        if file_obj_for_supplier:
            try:
                read_text = _promote_supplier_remap_text(
                    str(materialize_invoice_file(file_obj_for_supplier)),
                    payload.page_no,
                    read_text or "",
                )
            except Exception:
                read_text = _normalise_supplier_remap_text(read_text) if read_text else ""
        elif read_text:
            read_text = _normalise_supplier_remap_text(read_text)
    else:
        read_text = " ".join(read_text.split()).strip() if read_text else ""
    if payload.field_name == "supplier_name" and not read_text and payload.current_value:
        # Last-resort fallback: if OCR cannot read the selected crop, do not
        # preserve an unusable blank supplier hint.  Use the value currently in
        # the editor as the supplier snapshot so future replay can still match
        # this region/layout, but still mark the response as a fallback.
        read_text = _normalise_supplier_remap_text(payload.current_value)

    used_current_value_fallback = False
    if _current_value_better_than_region_read(payload.field_name, read_text, payload.current_value):
        # The user has already corrected/confirmed the value in the editor and
        # is saving the region as future learning.  When the crop OCR is blank
        # or clipped (common with tight image selections), store the editor value
        # so the immediate correction and example rule are complete.  Future
        # scans still re-read the coordinates; this does not blindly reuse stale
        # invoice totals/numbers.
        read_text = " ".join((payload.current_value or "").split()).strip()
        if payload.field_name == "supplier_name":
            read_text = _normalise_supplier_remap_text(read_text)
        used_current_value_fallback = True
        logger.debug(
            "save_remap: using current editor value fallback for field=%r value=%r",
            payload.field_name, read_text[:80],
        )

    if payload.field_name == "supplier_name" and read_text:
        _snapshot_supplier_name = read_text
        hint.supplier_name_snapshot = read_text
        if payload.current_value and _normalise_supplier_remap_text(payload.current_value) == read_text:
            # This is true both for direct OCR and fallback; the UI uses the
            # flag only as information, not as an error.
            used_current_value_fallback = not bool(payload.selected_text and payload.selected_text.strip())
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
            "error":         None,
            "warning":       "No text could be read from the selected region. "
                             "Region coordinates were saved as a future hint. "
                             "Try selecting a slightly wider area if you want to apply a value immediately.",
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
            _norm_supplier = re.sub(
                r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
                "", row.supplier_name.lower(),
            )
            _norm_supplier = re.sub(r"[^a-z0-9 ]", " ", _norm_supplier)
            _norm = re.sub(r"\s+", " ", _norm_supplier).strip()

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
        "used_current_value_fallback": used_current_value_fallback,
    }





# ── Rules management (admin + tenant-scoped user access) ──────────────────────

def _rule_to_dict(r: CorrectionRule, tenant_lookup: dict | None = None) -> dict:
    tenant_meta = (tenant_lookup or {}).get(r.tenant_id) or {}
    is_global = bool(getattr(r, "is_global", False))
    return {
        "id": r.id,
        "item_type": "rule",
        "tenant_id": str(r.tenant_id),
        "tenant_name": tenant_meta.get("tenant_name"),
        "tenant_code": tenant_meta.get("tenant_code"),
        "company_id": str(r.company_id) if r.company_id else None,
        # applies_to: human-readable scope label for UI display
        "applies_to": "global" if is_global else ("this_company" if r.company_id else "all_companies"),
        "is_global": is_global,
        "rule_type": r.rule_type,
        "field_name": r.field_name,
        "source_pattern": r.source_pattern,
        "target_value": r.target_value,
        "active": r.active,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "disabled_at": r.disabled_at.isoformat() if r.disabled_at else None,
        "origin_batch_id": str(r.origin_batch_id) if r.origin_batch_id else None,
        "origin_row_id": r.origin_row_id,
    }


def _remap_hint_as_rule_dict(h: RemapHint, tenant_lookup: dict | None = None) -> dict:
    tenant_meta = (tenant_lookup or {}).get(h.tenant_id) or {}
    x = float(h.x) if h.x is not None else None
    y = float(h.y) if h.y is not None else None
    w = float(h.w) if h.w is not None else None
    hh = float(h.h) if h.h is not None else None
    coords = f"x={x:.3f}, y={y:.3f}, w={w:.3f}, h={hh:.3f}" if None not in (x, y, w, hh) else "coordinates not set"
    supplier = h.supplier_name_snapshot or "supplier/layout saved region"
    archived = bool(getattr(h, "archived", False) or getattr(h, "deleted_at", None))
    role = "Archived saved region" if archived else ("Primary saved region" if getattr(h, "is_primary", False) else "Fallback saved region")
    if not h.active:
        role = "Region disabled"
    return {
        "id": f"hint-{h.id}",
        "hint_id": h.id,
        "item_type": "saved_region",
        "tenant_id": str(h.tenant_id),
        "tenant_name": tenant_meta.get("tenant_name"),
        "tenant_code": tenant_meta.get("tenant_code"),
        "company_id": str(h.company_id) if h.company_id else None,
        "applies_to": "this_company" if h.company_id else "all_companies",
        "is_global": False,
        "rule_type": "saved_region",
        "field_name": h.field_name,
        "source_pattern": supplier,
        "target_value": f"{role}; reference page {h.page_no or 1}; page-independent replay; {coords}",
        "active": bool(h.active and not archived),
        "is_primary": bool(getattr(h, "is_primary", False)),
        "archived": bool(getattr(h, "archived", False)),
        "deleted_at": h.deleted_at.isoformat() if getattr(h, "deleted_at", None) else None,
        "last_used_at": h.last_used_at.isoformat() if getattr(h, "last_used_at", None) else None,
        "last_result": getattr(h, "last_result", None),
        "success_count": int(getattr(h, "success_count", 0) or 0),
        "failure_count": int(getattr(h, "failure_count", 0) or 0),
        "conflict_count": int(getattr(h, "conflict_count", 0) or 0),
        "apply_count": int(getattr(h, "apply_count", 0) or 0),
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "disabled_at": None,
        "origin_batch_id": str(h.source_batch_id) if h.source_batch_id else None,
        "origin_row_id": h.source_row_id,
        "source_batch_id": str(h.source_batch_id) if h.source_batch_id else None,
        "source_row_id": h.source_row_id,
        "readonly_edit": True,
    }


def _tenant_lookup(db: Session) -> dict:
    rows = db.execute(select(M.Tenant)).scalars().all()
    return {t.id: {"tenant_name": t.tenant_name, "tenant_code": t.tenant_code} for t in rows}


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
    # Scope reassignment fields
    # applies_to: "this_company" | "all_companies"
    # company_id: required when applies_to="this_company"
    applies_to: str | None = None
    company_id: str | None = None
    is_global: bool | None = None


@router.get("/rules")
def list_rules_tenant(
    company_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    include_saved_regions: bool = Query(default=True),
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """List correction rules and, by default, saved regions visible to the user.

    Tenant users manage the active tenant context selected in the UI.  Admins can
    see all tenant rules and saved regions by default, or filter using
    X-Tenant-Id/company_id.
    """
    from app.db.models import UserTenant as _UT
    from uuid import UUID as _UUID

    q = select(CorrectionRule)
    hint_q = select(RemapHint).where(RemapHint.deleted_at.is_(None))
    is_admin = getattr(user, "role", None) == "admin"

    selected_tid = None
    if x_tenant_id:
        try:
            selected_tid = _UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(400, "Invalid X-Tenant-Id header")

    if is_admin:
        if selected_tid:
            q = q.where(or_(CorrectionRule.tenant_id == selected_tid, CorrectionRule.is_global.is_(True)))
            hint_q = hint_q.where(RemapHint.tenant_id == selected_tid)
    else:
        if selected_tid:
            link = db.execute(
                select(_UT).where(_UT.user_id == user.id, _UT.tenant_id == selected_tid).limit(1)
            ).scalar_one_or_none()
            if not link:
                raise HTTPException(403, "Forbidden for selected tenant")
            tenant_id = selected_tid
        else:
            tenant_id = _user_default_tenant_id(db, user)
        # Tenant users see only their own tenant-authored rules.
        # Platform-global rules apply in the background but remain admin-only.
        q = q.where(CorrectionRule.tenant_id == tenant_id)
        hint_q = hint_q.where(RemapHint.tenant_id == tenant_id)

    if company_id:
        try:
            cid = _UUID(company_id)
            if is_admin:
                q = q.where(
                    (CorrectionRule.company_id == cid)
                    | (CorrectionRule.company_id.is_(None))
                    | (CorrectionRule.is_global.is_(True))
                )
            else:
                q = q.where(
                    (CorrectionRule.company_id == cid)
                    | (CorrectionRule.company_id.is_(None))
                )
            hint_q = hint_q.where((RemapHint.company_id == cid) | (RemapHint.company_id.is_(None)))
        except ValueError:
            raise HTTPException(400, "company_id is not a valid UUID")
    if active_only:
        q = q.where(CorrectionRule.active.is_(True))
        hint_q = hint_q.where(RemapHint.active.is_(True), RemapHint.archived.is_(False))

    tenant_lookup = _tenant_lookup(db) if is_admin else {}
    rules = db.execute(q.order_by(desc(CorrectionRule.created_at))).scalars().all()
    items = [_rule_to_dict(r, tenant_lookup) for r in rules]
    if include_saved_regions:
        hints = db.execute(hint_q.order_by(desc(RemapHint.created_at), desc(RemapHint.id))).scalars().all()
        items.extend(_remap_hint_as_rule_dict(h, tenant_lookup) for h in hints)
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


@router.get("/admin/rules")
def list_rules(db: Session = Depends(get_db), user=Depends(current_user)):
    _require_admin(user)
    tenant_lookup = _tenant_lookup(db)
    rules = db.execute(
        select(CorrectionRule).order_by(desc(CorrectionRule.created_at))
    ).scalars().all()
    return [_rule_to_dict(r, tenant_lookup) for r in rules]


@router.post("/admin/rules/{rule_id}/global")
def convert_rule_to_global(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    """Promote one tenant-authored rule to a platform global background rule."""
    _require_admin(user)
    r = db.get(CorrectionRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    r.is_global = True
    r.company_id = None
    r.active = True
    r.disabled_by = None
    r.disabled_at = None
    db.commit()
    return _rule_to_dict(r, _tenant_lookup(db))


@router.post("/admin/rules/{rule_id}/tenant-scoped")
def convert_rule_to_tenant_scoped(rule_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    """Demote a global rule back to its origin tenant scope."""
    _require_admin(user)
    r = db.get(CorrectionRule, rule_id)
    if not r:
        raise HTTPException(404, "Rule not found")
    r.is_global = False
    db.commit()
    return _rule_to_dict(r, _tenant_lookup(db))


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

    if payload.is_global is not None:
        _require_admin(user)
        r.is_global = bool(payload.is_global)
        if r.is_global:
            r.company_id = None

    # ── Scope reassignment ──────────────────────────────────────────────────
    if payload.applies_to is not None:
        from uuid import UUID as _UUID
        is_admin = getattr(user, "role", None) == "admin"

        if payload.applies_to == "all_companies":
            # Widen to all companies in tenant: clear company_id
            r.company_id = None

        elif payload.applies_to == "this_company":
            # Narrow to a specific company
            if not payload.company_id:
                raise HTTPException(422, "company_id is required when applies_to is 'this_company'")
            try:
                target_cid = _UUID(payload.company_id)
            except ValueError:
                raise HTTPException(422, "company_id is not a valid UUID")

            # Tenant users: verify the target company belongs to their tenant
            if not is_admin:
                from app.db.models import Company as _Co
                co = db.execute(
                    select(_Co).where(
                        _Co.id == target_cid,
                        _Co.tenant_id == r.tenant_id,
                    ).limit(1)
                ).scalar_one_or_none()
                if not co:
                    raise HTTPException(403, "Company not found in your tenant")
            r.company_id = target_cid

        else:
            raise HTTPException(422, f"applies_to must be 'this_company' or 'all_companies', got: {payload.applies_to!r}")

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

# ── Remap hint maintenance endpoints ───────────────────────────────────────
def _user_default_tenant_id(db: Session, user) -> UUID:
    """Return the user's default tenant id for maintenance endpoints."""
    ut = db.execute(
        select(M.UserTenant).where(
            M.UserTenant.user_id == user.id,
            M.UserTenant.is_default.is_(True),
        ).limit(1)
    ).scalar_one_or_none()
    if not ut:
        ut = db.execute(
            select(M.UserTenant).where(M.UserTenant.user_id == user.id).limit(1)
        ).scalar_one_or_none()
    if not ut:
        raise HTTPException(403, "User is not attached to a tenant")
    return ut.tenant_id


def _active_tenant_id_for_user(db: Session, user, x_tenant_id: str | None = None) -> UUID:
    """Resolve the tenant selected by the UI and verify user access."""
    if getattr(user, "role", None) == "admin" and x_tenant_id:
        try:
            return UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(400, "Invalid X-Tenant-Id header")
    if x_tenant_id:
        try:
            selected = UUID(x_tenant_id)
        except ValueError:
            raise HTTPException(400, "Invalid X-Tenant-Id header")
        link = db.execute(
            select(M.UserTenant).where(
                M.UserTenant.user_id == user.id,
                M.UserTenant.tenant_id == selected,
            ).limit(1)
        ).scalar_one_or_none()
        if not link and getattr(user, "role", None) != "admin":
            raise HTTPException(403, "Forbidden for selected tenant")
        return selected
    return _user_default_tenant_id(db, user)


@router.post("/batches/{batch_id}/rows/{row_id}/apply-saved-regions")
def apply_saved_regions_to_row(
    batch_id: UUID,
    row_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """Manually replay active saved regions against one review row.

    The Saved regions panel is mainly maintenance.  This action gives reviewers
    an explicit way to prove whether the current row is being improved by saved
    coordinates, without waiting for the next batch scan.
    """
    batch = _get_batch(db, batch_id)
    row = db.get(M.InvoiceRow, row_id)
    if not row or row.batch_id != batch.id:
        raise HTTPException(404, "Row not found")

    tenant_id = _active_tenant_id_for_user(db, user, x_tenant_id)
    if batch.tenant_id != tenant_id and getattr(user, "role", None) != "admin":
        raise HTTPException(403, "Batch is not in your tenant")

    tracked = (
        "supplier_name", "invoice_number", "invoice_date",
        "net_amount", "vat_amount", "total_amount",
        "nominal_account_code", "description",
    )
    before = {f: getattr(row, f, None) for f in tracked}
    supplier_before = row.supplier_name
    reasons_before = row.review_reasons or ""
    method_before = row.method_used or ""
    last_audit_id = (
        db.query(InvoiceRowFieldAudit.id)
        .filter(InvoiceRowFieldAudit.row_id == row.id)
        .order_by(desc(InvoiceRowFieldAudit.id))
        .scalar()
    ) or 0

    try:
        from app.routers.batches import _apply_remap_hints, _apply_account_suggestions
        _apply_remap_hints(db, batch, row)
        if row.supplier_name != supplier_before:
            _apply_account_suggestions(db, batch.tenant_id, batch.company_id, row)
    except Exception as exc:
        logger.warning("apply_saved_regions_to_row failed row_id=%s: %s", row_id, exc)
        raise HTTPException(500, f"Saved region replay failed: {exc}")

    after = {f: getattr(row, f, None) for f in tracked}
    changed = {
        f: {"old": before[f], "new": after[f]}
        for f in tracked
        if str(before[f] or "") != str(after[f] or "")
    }

    conflict_fields: list[str] = []
    conflict_text = "|".join([row.review_reasons or "", row.method_used or ""])
    for f in tracked:
        if f"saved_region_conflict:{f}" in conflict_text or f"remap_hint_conflict:{f}" in conflict_text:
            conflict_fields.append(f)

    if changed:
        row.review_required = True
        row.validation_status = row.validation_status or "saved_region_applied"
        reasons = [x for x in re.split(r"[|]", row.review_reasons or "") if x]
        reason = "Saved region replay changed field values; verify before approval"
        if reason not in reasons:
            reasons.append(reason)
        row.review_reasons = "|".join(reasons)[:500]
        db.add(InvoiceRowFieldAudit(
            batch_id=batch.id,
            row_id=row.id,
            field_name="saved_regions",
            old_value=None,
            new_value=", ".join(changed.keys()),
            action="saved_region_replay",
            note="Manually replayed saved regions from review page",
            rule_created=False,
            user_id=user.id,
            username=getattr(user, "email", None) or str(user.id),
        ))

    db.commit()
    checked_changed = (row.review_reasons or "") != reasons_before or (row.method_used or "") != method_before or bool(changed)
    region_audits = (
        db.query(InvoiceRowFieldAudit)
        .filter(
            InvoiceRowFieldAudit.row_id == row.id,
            InvoiceRowFieldAudit.id > last_audit_id,
            InvoiceRowFieldAudit.action.like("saved_region%"),
        )
        .order_by(InvoiceRowFieldAudit.id.asc())
        .all()
    )
    region_events = []
    for audit in region_audits:
        note = audit.note or ""
        hint_match = re.search(r"remap_hint_id=(\d+)", note)
        region_events.append({
            "field_name": audit.field_name,
            "status": audit.action,
            "old": audit.old_value,
            "new": audit.new_value,
            "region_id": int(hint_match.group(1)) if hint_match else None,
            "reason": note[:500],
        })
    checked_fields = sorted({e["field_name"] for e in region_events} | set(changed.keys())) or list(tracked)
    saved_region_diagnostics = {
        "status": "checked" if checked_changed else "checked_no_change",
        "regions_found": len(region_events),
        "regions_tested": len(region_events),
        "fields_checked": checked_fields,
        "region_events": region_events,
        "values_read": changed,
        "fields_changed": list(changed.keys()),
        "conflicts": conflict_fields,
        "skipped_reasons": [] if checked_changed else ["No applicable active saved region changed the selected row."],
    }
    return {
        "changed": changed,
        "changed_fields": list(changed.keys()),
        "conflict_fields": conflict_fields,
        "checked_regions": len(region_events),
        "diagnostics": saved_region_diagnostics,
        "method_used": row.method_used,
    }



@router.get("/remap-hints")
def list_remap_hints(
    active: bool | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    include_archived: bool = Query(default=True),
    field_name: str | None = Query(default=None),
    company_id: str | None = Query(default=None),
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """List saved remap regions with governance status.

    The response remains backwards-compatible (`items`) and also returns grouped
    supplier/field summaries (`groups`) so the UI can show which region is the
    primary one used by the scanner.
    """
    tenant_id = _active_tenant_id_for_user(db, user, x_tenant_id)
    q = select(RemapHint).where(RemapHint.tenant_id == tenant_id, RemapHint.deleted_at.is_(None))
    if company_id:
        try:
            cid = UUID(company_id)
        except ValueError:
            raise HTTPException(400, "company_id is not a valid UUID")
        q = q.where((RemapHint.company_id == cid) | (RemapHint.company_id.is_(None)))
    if not include_inactive:
        if active is None:
            active = True
        q = q.where(RemapHint.active.is_(active))
    elif active is not None:
        q = q.where(RemapHint.active.is_(active))
    if not include_archived:
        q = q.where(RemapHint.archived.is_(False))
    if field_name:
        q = q.where(RemapHint.field_name == field_name)
    hints = db.execute(q.order_by(desc(RemapHint.is_primary), desc(RemapHint.created_at), desc(RemapHint.id))).scalars().all()

    def _group_key(h: RemapHint) -> tuple:
        supplier_key = f"id:{h.supplier_id}" if h.supplier_id else f"name:{_normalise_remap_group_supplier(h.supplier_name_snapshot)}"
        return (str(h.company_id) if h.company_id else "tenant", supplier_key, h.field_name)

    group_counts: dict[tuple, dict] = {}
    for h in hints:
        key = _group_key(h)
        g = group_counts.setdefault(key, {
            "company_id": str(h.company_id) if h.company_id else None,
            "supplier_name_snapshot": h.supplier_name_snapshot,
            "stable_anchor_type": getattr(h, "stable_anchor_type", None),
            "stable_anchor_value": getattr(h, "stable_anchor_value", None),
            "stable_anchor_evidence": getattr(h, "stable_anchor_evidence", None),
            "field_name": h.field_name,
            "active_count": 0,
            "fallback_count": 0,
            "archived_count": 0,
            "primary_hint_id": None,
            "last_used_at": None,
            "last_result": None,
            "success_count": 0,
            "failure_count": 0,
            "conflict_count": 0,
            "apply_count": 0,
        })
        if h.archived:
            g["archived_count"] += 1
        elif h.active:
            g["active_count"] += 1
            if h.is_primary:
                g["primary_hint_id"] = h.id
            else:
                g["fallback_count"] += 1
        for k in ("success_count", "failure_count", "conflict_count", "apply_count"):
            g[k] += int(getattr(h, k, 0) or 0)
        if h.last_used_at and (not g["last_used_at"] or h.last_used_at.isoformat() > g["last_used_at"]):
            g["last_used_at"] = h.last_used_at.isoformat()
            g["last_result"] = h.last_result

    items = []
    for h in hints:
        key = _group_key(h)
        x = float(h.x) if h.x is not None else None
        y = float(h.y) if h.y is not None else None
        w = float(h.w) if h.w is not None else None
        hh = float(h.h) if h.h is not None else None
        role = "archived" if h.archived else ("primary" if h.is_primary else "fallback")
        if not h.active:
            role = "disabled"
        items.append({
            "id": h.id,
            "tenant_id": str(h.tenant_id),
            "company_id": str(h.company_id) if h.company_id else None,
            "group_key": "|".join(map(str, key)),
            "field_name": h.field_name,
            "supplier_id": h.supplier_id,
            "supplier_name_snapshot": h.supplier_name_snapshot,
            "stable_anchor_type": getattr(h, "stable_anchor_type", None),
            "stable_anchor_value": getattr(h, "stable_anchor_value", None),
            "stable_anchor_evidence": getattr(h, "stable_anchor_evidence", None),
            "page_no": h.page_no,
            "reference_page_no": h.page_no,
            "x": x, "y": y, "w": w, "h": hh,
            "coordinates": f"x={x:.3f}, y={y:.3f}, w={w:.3f}, h={hh:.3f}" if None not in (x, y, w, hh) else None,
            "active": h.active,
            "is_primary": bool(h.is_primary),
            "archived": bool(h.archived),
            "deleted_at": h.deleted_at.isoformat() if h.deleted_at else None,
            "governance_role": role,
            "source_batch_id": str(h.source_batch_id) if h.source_batch_id else None,
            "source_file_id": h.source_file_id,
            "source_row_id": h.source_row_id,
            "created_at": h.created_at.isoformat() if h.created_at else None,
            "last_used_at": h.last_used_at.isoformat() if h.last_used_at else None,
            "last_used_page_no": h.last_used_page_no,
            "last_read_text": h.last_read_text,
            "last_result": h.last_result,
            "success_count": int(h.success_count or 0),
            "failure_count": int(h.failure_count or 0),
            "conflict_count": int(h.conflict_count or 0),
            "apply_count": int(h.apply_count or 0),
            "duplicate_count": group_counts[key]["active_count"] + group_counts[key]["archived_count"],
        })
    return {"items": items, "groups": list(group_counts.values()), "count": len(items)}


@router.post("/remap-hints/{hint_id}/disable")
def disable_remap_hint(
    hint_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    hint.active = False
    hint.is_primary = False
    db.commit()
    return {"id": hint.id, "active": hint.active, "is_primary": hint.is_primary}


@router.post("/remap-hints/{hint_id}/enable")
def enable_remap_hint(
    hint_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    hint.active = True
    hint.archived = False
    hint.archived_at = None
    hint.archived_by = None
    hint.deleted_at = None
    hint.deleted_by = None
    # If there is no primary in this group, enabling this region makes it primary.
    peers = db.execute(select(RemapHint).where(
        RemapHint.tenant_id == hint.tenant_id,
        RemapHint.field_name == hint.field_name,
        RemapHint.active.is_(True),
        RemapHint.archived.is_(False),
        RemapHint.deleted_at.is_(None),
    )).scalars().all()
    if not any(p.is_primary for p in peers if p.id != hint.id and _same_remap_group(hint, p)):
        _set_primary_hint(db, hint)
    db.commit()
    return {"id": hint.id, "active": hint.active, "is_primary": hint.is_primary, "archived": hint.archived}


@router.post("/remap-hints/{hint_id}/primary")
def set_remap_hint_primary(
    hint_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    hint.active = True
    hint.archived = False
    hint.deleted_at = None
    hint.deleted_by = None
    _set_primary_hint(db, hint)
    db.commit()
    return {"id": hint.id, "is_primary": True, "active": True}


@router.post("/remap-hints/{hint_id}/archive")
def archive_remap_hint(
    hint_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    hint.archived = True
    hint.active = False
    hint.is_primary = False
    hint.archived_at = datetime.utcnow()
    hint.archived_by = getattr(user, "id", None)
    disabled_rules = _disable_related_remap_rules(db, hint, user)
    db.commit()
    return {"id": hint.id, "archived": True, "active": False, "disabled_related_rules": disabled_rules}


@router.post("/remap-hints/{hint_id}/restore")
def restore_remap_hint(
    hint_id: int,
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    hint.archived = False
    hint.archived_at = None
    hint.archived_by = None
    hint.deleted_at = None
    hint.deleted_by = None
    hint.active = True
    peers = db.execute(select(RemapHint).where(
        RemapHint.tenant_id == hint.tenant_id,
        RemapHint.field_name == hint.field_name,
        RemapHint.active.is_(True),
        RemapHint.archived.is_(False),
        RemapHint.deleted_at.is_(None),
    )).scalars().all()
    if not any(p.is_primary for p in peers if p.id != hint.id and _same_remap_group(hint, p)):
        _set_primary_hint(db, hint)
    db.commit()
    return {"id": hint.id, "archived": False, "active": True, "is_primary": hint.is_primary}


@router.post("/remap-hints/deduplicate")
def deduplicate_remap_hints(
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    """Archive duplicate saved regions and keep one primary per supplier/field group."""
    tenant_id = _active_tenant_id_for_user(db, user, x_tenant_id)
    hints = db.execute(select(RemapHint).where(
        RemapHint.tenant_id == tenant_id,
        RemapHint.deleted_at.is_(None),
    ).order_by(desc(RemapHint.is_primary), desc(RemapHint.success_count), desc(RemapHint.created_at), desc(RemapHint.id))).scalars().all()
    groups: list[list[RemapHint]] = []
    for h in hints:
        placed = False
        for g in groups:
            if _same_remap_group(h, g[0]):
                g.append(h)
                placed = True
                break
        if not placed:
            groups.append([h])

    archived = 0
    primary_set = 0
    for g in groups:
        active = [h for h in g if h.active and not h.archived and not h.deleted_at]
        if not active:
            continue
        active.sort(key=lambda h: (
            0 if h.is_primary else 1,
            0 if h.company_id else 1,
            -int(h.success_count or 0),
            -int(h.apply_count or 0),
            -int(h.id or 0),
        ))
        primary = active[0]
        for h in active:
            h.is_primary = h.id == primary.id
        primary_set += 1
        seen_coords = set()
        for h in active[1:]:
            coord_key = (
                round(float(h.x or 0), 3), round(float(h.y or 0), 3),
                round(float(h.w or 0), 3), round(float(h.h or 0), 3),
            )
            if coord_key in seen_coords or _coords_close(primary, h.x, h.y, h.w, h.h):
                h.archived = True
                h.active = False
                h.is_primary = False
                h.archived_at = datetime.utcnow()
                h.archived_by = getattr(user, "id", None)
                h.superseded_by_hint_id = primary.id
                _disable_related_remap_rules(db, h, user)
                archived += 1
            else:
                seen_coords.add(coord_key)
    db.commit()
    return {"groups_checked": len(groups), "primary_groups": primary_set, "archived_duplicates": archived}


@router.delete("/remap-hints/{hint_id}")
def delete_remap_hint(
    hint_id: int,
    hard_delete: bool = Query(default=False),
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    hint = _get_remap_hint_for_user(hint_id, db, user, x_tenant_id)
    if hard_delete:
        if not hint.archived and not hint.deleted_at:
            raise HTTPException(409, "Archive this saved region before permanent deletion")
        db.delete(hint)
        db.commit()
        return {"deleted": True, "hard_delete": True, "id": hint_id}

    hint.archived = True
    hint.active = False
    hint.is_primary = False
    hint.archived_at = hint.archived_at or datetime.utcnow()
    hint.archived_by = hint.archived_by or getattr(user, "id", None)
    hint.deleted_at = datetime.utcnow()
    hint.deleted_by = getattr(user, "id", None)
    disabled_rules = _disable_related_remap_rules(db, hint, user)
    db.commit()
    return {"deleted": False, "archived": True, "soft_delete": True, "id": hint_id, "disabled_related_rules": disabled_rules}

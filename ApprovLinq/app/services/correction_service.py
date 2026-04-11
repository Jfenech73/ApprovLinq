"""Correction overlay + audit + status lifecycle service.

ALL row edits go through this module so that:
- raw InvoiceRow stays immutable
- every change is audited
- controlled-list rules for supplier/nominal are enforced
- status transitions are guarded
"""
from __future__ import annotations
from datetime import datetime, date
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import (
    InvoiceRowCorrection, InvoiceRowFieldAudit, CorrectionRule, CORRECTABLE_FIELDS,
)

ALLOWED_TRANSITIONS = {
    "scanned":   {"in_review"},
    "created":   {"in_review"},
    "in_review": {"approved", "in_review"},
    "approved":  {"exported", "in_review"},
    "exported":  set(),  # only via reopen()
}


def normalise_status(s: str | None) -> str:
    if s in (None, "", "created"):
        return "scanned"
    return s


def get_or_create_correction(db: Session, row: M.InvoiceRow) -> InvoiceRowCorrection:
    c = db.get(InvoiceRowCorrection, row.id)
    if c is None:
        c = InvoiceRowCorrection(row_id=row.id, batch_id=row.batch_id)
        db.add(c)
        db.flush()
    return c


def effective_value(row: M.InvoiceRow, c: InvoiceRowCorrection | None, field: str) -> Any:
    if c is not None:
        v = getattr(c, field, None)
        if v is not None:
            return v
    return getattr(row, field, None)


def _coerce(field: str, raw: Any) -> Any:
    if raw in (None, ""):
        return None
    if field in ("net_amount", "vat_amount", "total_amount"):
        return float(raw)
    if field == "invoice_date":
        return raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    return str(raw)


def apply_field_changes(
    db: Session,
    *,
    batch: M.InvoiceBatch,
    row: M.InvoiceRow,
    changes: dict[str, Any],
    user: M.User,
    note: str | None = None,
    force_add: bool = False,
    save_as_rule_fields: set[str] | None = None,
) -> list[InvoiceRowFieldAudit]:
    if normalise_status(batch.status) == "exported":
        raise PermissionError("Batch is exported; reopen first.")
    save_as_rule_fields = save_as_rule_fields or set()
    correction = get_or_create_correction(db, row)
    audits: list[InvoiceRowFieldAudit] = []

    for field, raw_new in changes.items():
        if field not in CORRECTABLE_FIELDS:
            continue
        new_val = _coerce(field, raw_new)
        old_val = effective_value(row, correction, field)
        if (old_val if old_val not in ("",) else None) == (new_val if new_val not in ("",) else None):
            continue

        force_added_flag = False
        if field == "supplier_name" and new_val:
            exists = db.execute(
                select(M.TenantSupplier).where(
                    M.TenantSupplier.tenant_id == batch.tenant_id,
                    M.TenantSupplier.name == new_val,
                )
            ).scalar_one_or_none()
            if not exists:
                if not (force_add and note):
                    raise ValueError("supplier_name not in master list; force_add=true and note required")
                db.add(M.TenantSupplier(tenant_id=batch.tenant_id, name=new_val))
                force_added_flag = True
        if field == "nominal_account_code" and new_val:
            exists = db.execute(
                select(M.TenantNominalAccount).where(
                    M.TenantNominalAccount.tenant_id == batch.tenant_id,
                    M.TenantNominalAccount.code == new_val,
                )
            ).scalar_one_or_none()
            if not exists:
                if not (force_add and note):
                    raise ValueError("nominal_account_code not in master list; force_add=true and note required")
                db.add(M.TenantNominalAccount(tenant_id=batch.tenant_id, code=new_val))
                force_added_flag = True

        setattr(correction, field, new_val)

        rule_created = False
        if field in save_as_rule_fields and field in ("supplier_name", "nominal_account_code") and new_val:
            src = (str(row.supplier_name or "") if field == "supplier_name"
                   else str(row.nominal_account_code or "")).strip().lower()
            if src and src != str(new_val).strip().lower():
                db.add(CorrectionRule(
                    tenant_id=batch.tenant_id,
                    company_id=batch.company_id,
                    rule_type="supplier_alias" if field == "supplier_name" else "nominal_remap",
                    field_name=field,
                    source_pattern=src,
                    target_value=str(new_val),
                    created_by=user.id,
                    origin_batch_id=batch.id,
                    origin_row_id=row.id,
                ))
                rule_created = True

        a = InvoiceRowFieldAudit(
            batch_id=batch.id, row_id=row.id, field_name=field,
            old_value=None if old_val is None else str(old_val),
            new_value=None if new_val is None else str(new_val),
            action="edit", note=note,
            rule_created=rule_created, force_added=force_added_flag,
            user_id=user.id,
            username=getattr(user, "email", None) or getattr(user, "full_name", None),
        )
        db.add(a)
        audits.append(a)

    correction.updated_at = datetime.utcnow()
    correction.updated_by = user.id
    if normalise_status(batch.status) == "scanned" and audits:
        batch.status = "in_review"
    return audits


def revert_field(db: Session, *, batch, row, field: str, user, note: str | None = None):
    correction = db.get(InvoiceRowCorrection, row.id)
    if correction is None or getattr(correction, field, None) is None:
        return None
    old = getattr(correction, field)
    setattr(correction, field, None)
    a = InvoiceRowFieldAudit(
        batch_id=batch.id, row_id=row.id, field_name=field,
        old_value=str(old), new_value=None, action="revert",
        note=note, user_id=user.id,
    )
    db.add(a)
    return a


def mark_field_reviewed(db, *, batch, row, field, user):
    c = get_or_create_correction(db, row)
    cur = set((c.reviewed_fields or "").split(",")) - {""}
    cur.add(field)
    c.reviewed_fields = ",".join(sorted(cur))
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id, row_id=row.id, field_name=field,
        old_value=None, new_value="reviewed", action="mark_reviewed", user_id=user.id))


def transition_status(db: Session, *, batch: M.InvoiceBatch, target: str, user: M.User):
    cur = normalise_status(batch.status)
    allowed = ALLOWED_TRANSITIONS.get(cur, set())
    if target not in allowed:
        raise ValueError(f"Illegal transition: {cur} -> {target}")
    batch.status = target
    if target == "approved":
        batch.approved_at = datetime.utcnow()
        batch.approved_by = user.id
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id, row_id=0, field_name="__status__",
        old_value=cur, new_value=target, action="status", user_id=user.id))


def reopen_batch(db: Session, *, batch: M.InvoiceBatch, user: M.User):
    if normalise_status(batch.status) != "exported":
        raise ValueError("Only exported batches can be reopened")
    batch.status = "in_review"
    batch.reopened_at = datetime.utcnow()
    batch.reopened_by = user.id
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id, row_id=0, field_name="__status__",
        old_value="exported", new_value="in_review", action="reopen", user_id=user.id))


def load_correction_map(db: Session, batch_id) -> dict[int, InvoiceRowCorrection]:
    rows = db.execute(
        select(InvoiceRowCorrection).where(InvoiceRowCorrection.batch_id == batch_id)
    ).scalars().all()
    return {c.row_id: c for c in rows}

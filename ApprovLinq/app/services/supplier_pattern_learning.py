from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow, SupplierPattern, TenantSupplier, User
from app.db.review_models import InvoiceRowCorrection


TRUSTED_PATTERN_OUTCOME_SOURCES: frozenset[str] = frozenset({
    "manual_review",
    "mark_reviewed",
    "approved",
    "export",
})

PATTERN_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "of", "for", "a", "an", "in", "on", "at", "to", "by", "is",
    "are", "was", "with", "from", "that", "this", "ltd", "limited", "plc",
    "invoice", "date", "page", "number", "vat", "tax", "total", "amount",
    "description", "quantity", "price", "unit", "subtotal", "balance", "ref",
    "your", "our", "due", "paid", "name", "address", "account",
})


def extract_pattern_keywords(text: str) -> set[str]:
    """Return meaningful lowercase words from invoice header text."""
    words = re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
    return {w for w in words if w not in PATTERN_STOP_WORDS}


def _keyword_text(keywords: set[str], limit: int = 60) -> str:
    return " ".join(sorted(keywords)[:limit])


def _merge_keyword_text(existing: str | None, new_keywords: set[str], limit: int = 60) -> str:
    current = set((existing or "").split())
    return _keyword_text(current | new_keywords, limit=limit)


def _supplier_for_row_value(
    db: Session,
    *,
    tenant_id: Any,
    company_id: Any,
    supplier_name: str | None,
) -> TenantSupplier | None:
    if not tenant_id or not company_id or not supplier_name:
        return None
    return (
        db.query(TenantSupplier)
        .filter(
            TenantSupplier.tenant_id == tenant_id,
            TenantSupplier.company_id == company_id,
            TenantSupplier.supplier_name == supplier_name,
            TenantSupplier.is_active.is_(True),
        )
        .first()
    )


def _final_supplier_name(db: Session, row: InvoiceRow) -> str | None:
    correction = db.get(InvoiceRowCorrection, row.id) if row.id else None
    if correction and correction.supplier_name:
        return correction.supplier_name
    return row.supplier_name


def match_supplier_by_active_pattern(
    db: Session,
    tenant_id: Any,
    company_id: Any,
    header_text: str,
) -> TenantSupplier | None:
    """Return a supplier only from active, trusted keyword fingerprints."""
    if not header_text:
        return None

    keywords = extract_pattern_keywords(header_text)
    if len(keywords) < 3:
        return None

    patterns = (
        db.query(SupplierPattern)
        .filter(
            SupplierPattern.tenant_id == tenant_id,
            SupplierPattern.company_id == company_id,
            SupplierPattern.status == "active",
        )
        .all()
    )

    best_supplier: TenantSupplier | None = None
    best_score = 0.0
    for pattern in patterns:
        if not pattern.keywords:
            continue
        pattern_kws = set(pattern.keywords.split())
        if len(pattern_kws) < 3:
            continue
        overlap = keywords & pattern_kws
        if len(overlap) < 3:
            continue
        score = len(overlap) / max(len(pattern_kws), 1)
        if score >= 0.50 and score > best_score:
            supplier = db.get(TenantSupplier, pattern.supplier_id)
            if supplier and supplier.is_active:
                best_score = score
                best_supplier = supplier
    return best_supplier


def record_supplier_pattern_proposals_for_batch(
    db: Session,
    *,
    batch_id: Any,
    tenant_id: Any,
    company_id: Any,
    scan_run_id: Any = None,
) -> int:
    """Record untrusted scan discoveries as inactive proposals only.

    This must never create or mutate active pattern keywords. Trusted promotion
    is handled by explicit review, approval or export outcomes.
    """
    q = db.query(InvoiceRow).filter(
        InvoiceRow.batch_id == batch_id,
        InvoiceRow.supplier_name.isnot(None),
        InvoiceRow.header_raw.isnot(None),
    )
    if scan_run_id is not None:
        q = q.filter(InvoiceRow.scan_run_id == scan_run_id)
    rows = q.all()
    changed = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        supplier = _supplier_for_row_value(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            supplier_name=row.supplier_name,
        )
        if not supplier:
            continue
        keywords = extract_pattern_keywords(row.header_raw or "")
        if len(keywords) < 3:
            continue

        existing = (
            db.query(SupplierPattern)
            .filter(
                SupplierPattern.tenant_id == tenant_id,
                SupplierPattern.company_id == company_id,
                SupplierPattern.supplier_id == supplier.id,
            )
            .first()
        )
        if existing:
            existing.proposed_keywords = _merge_keyword_text(existing.proposed_keywords, keywords)
            existing.proposal_count = (existing.proposal_count or 0) + 1
            existing.last_proposed_at = now
        else:
            db.add(
                SupplierPattern(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    supplier_id=supplier.id,
                    keywords=None,
                    hit_count=0,
                    status="proposed",
                    proposed_keywords=_keyword_text(keywords),
                    proposal_count=1,
                    last_proposed_at=now,
                    source_batch_id=batch_id,
                    source_row_id=row.id,
                )
            )
        changed += 1
    return changed


def promote_supplier_pattern_from_row(
    db: Session,
    *,
    batch: InvoiceBatch,
    row: InvoiceRow,
    user: User | None = None,
    outcome_source: str,
) -> bool:
    """Promote one row's supplier fingerprint after a trusted outcome."""
    if outcome_source not in TRUSTED_PATTERN_OUTCOME_SOURCES:
        return False
    supplier_name = _final_supplier_name(db, row)
    supplier = _supplier_for_row_value(
        db,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        supplier_name=supplier_name,
    )
    if not supplier:
        return False
    keywords = extract_pattern_keywords(row.header_raw or "")
    if len(keywords) < 3:
        return False

    now = datetime.now(timezone.utc)
    pattern = (
        db.query(SupplierPattern)
        .filter(
            SupplierPattern.tenant_id == batch.tenant_id,
            SupplierPattern.company_id == batch.company_id,
            SupplierPattern.supplier_id == supplier.id,
        )
        .first()
    )
    user_id = getattr(user, "id", None) if user is not None else None

    if pattern:
        was_active = pattern.status == "active"
        pattern.status = "active"
        pattern.keywords = _merge_keyword_text(pattern.keywords, keywords)
        pattern.hit_count = (pattern.hit_count or 0) + 1
        pattern.last_seen_at = now
        if not was_active or not pattern.activated_at:
            pattern.activated_at = now
            pattern.activated_by = user_id
        if not pattern.created_by:
            pattern.created_by = user_id
    else:
        pattern = SupplierPattern(
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            supplier_id=supplier.id,
            keywords=_keyword_text(keywords),
            hit_count=1,
            last_seen_at=now,
            status="active",
            created_by=user_id,
            activated_at=now,
            activated_by=user_id,
        )
        db.add(pattern)

    pattern.source_batch_id = batch.id
    pattern.source_row_id = row.id
    pattern.trusted_outcome_source = outcome_source[:40]
    pattern.last_trusted_at = now
    return True


def promote_supplier_patterns_for_batch(
    db: Session,
    *,
    batch: InvoiceBatch,
    user: User | None = None,
    outcome_source: str,
) -> int:
    """Promote all eligible supplier fingerprints in a trusted batch outcome."""
    if outcome_source not in TRUSTED_PATTERN_OUTCOME_SOURCES:
        return 0
    q = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch.id)
    if getattr(batch, "current_scan_run_id", None) is not None:
        q = q.filter(InvoiceRow.scan_run_id == batch.current_scan_run_id)
    rows = q.all()
    promoted = 0
    for row in rows:
        if promote_supplier_pattern_from_row(db, batch=batch, row=row, user=user, outcome_source=outcome_source):
            promoted += 1
    return promoted

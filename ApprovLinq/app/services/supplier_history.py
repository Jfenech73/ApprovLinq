"""Supplier-history learning helpers for deterministic invoice arbitration.

This module deliberately avoids any ML dependency.  It mines only accepted
historical evidence (reviewed corrections, exported batches, saved regions and
accepted rule applications) inside the same tenant/company boundary and returns
explainable candidates that the arbitration layer may apply or surface as
suggestions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import re
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow
from app.db.review_models import CorrectionRule, InvoiceRowCorrection, InvoiceRowFieldAudit, RemapHint

HISTORY_FIELDS: tuple[str, ...] = (
    "supplier_name",
    "invoice_number",
    "invoice_date",
    "net_amount",
    "vat_amount",
    "total_amount",
    "tax_code",
    "nominal_account_code",
    "description",
    "currency",
)
STABLE_HISTORY_FIELDS: set[str] = {
    "supplier_name",
    "tax_code",
    "nominal_account_code",
    "description",
    "currency",
}
PATTERN_ONLY_FIELDS: set[str] = {"invoice_number", "invoice_date", "net_amount", "vat_amount", "total_amount"}


@dataclass
class SupplierHistorySignal:
    field_name: str
    value: Any
    confidence: float
    support_count: int
    conflict_count: int = 0
    source_type: str = "supplier_history"
    source_row_id: int | None = None
    evidence: str = ""
    reason: str = ""
    company_specific: bool = False
    should_apply: bool = False


@dataclass
class SupplierHistoryProfile:
    tenant_id: Any
    company_id: Any | None
    supplier_key: str
    matched_row_count: int = 0
    accepted_row_count: int = 0
    company_accepted_row_count: int = 0
    signals: list[SupplierHistorySignal] = field(default_factory=list)
    conflicts: dict[str, int] = field(default_factory=dict)


def normalise_supplier_key(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()).lower()
    text = re.sub(
        r"\b(ltd|limited|plc|llc|inc|corp|co|company|group|trading|holdings|services|solutions)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: Any, b: Any) -> float:
    aa, bb = normalise_supplier_key(a), normalise_supplier_key(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.92
    return SequenceMatcher(None, aa, bb).ratio()


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, Decimal):
            return value.quantize(Decimal("0.01"))
        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(Decimal("0.01"))
        text = re.sub(r"[^0-9,\.\-]", "", str(value).strip())
        if not text or text in {"-", ".", ","}:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _value_key(field_name: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    if field_name in {"net_amount", "vat_amount", "total_amount"}:
        m = _money(value)
        return str(m) if m is not None else ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if field_name == "supplier_name":
        return normalise_supplier_key(value)
    return re.sub(r"\s+", " ", str(value).strip()).lower()


def _display_value(field_name: str, value: Any) -> Any:
    if field_name in {"net_amount", "vat_amount", "total_amount"}:
        m = _money(value)
        return float(m) if m is not None else value
    if isinstance(value, datetime):
        return value.date()
    return value


def _is_row_accepted(db: Session, row: InvoiceRow, correction: InvoiceRowCorrection | None = None) -> bool:
    """Only learn from explicit acceptance signals, not unreviewed raw rows."""
    if correction and correction.row_reviewed:
        return True
    # Rows in exported batches have passed the user's export gate.
    batch = db.get(InvoiceBatch, row.batch_id) if row.batch_id else None
    if (
        batch
        and str(batch.status or "").lower() == "exported"
        and (
            getattr(batch, "current_scan_run_id", None) is None
            or getattr(row, "scan_run_id", None) == getattr(batch, "current_scan_run_id", None)
        )
    ):
        return True
    # Explicit mark-reviewed audit is also an acceptance signal.
    if row.id:
        audit = db.execute(
            select(InvoiceRowFieldAudit.id)
            .where(
                InvoiceRowFieldAudit.row_id == row.id,
                InvoiceRowFieldAudit.action.in_(["mark_reviewed", "manual", "correction", "field_update"]),
            )
            .limit(1)
        ).first()
        if audit:
            return True
    return False


def _accepted_value_for_field(row: InvoiceRow, correction: InvoiceRowCorrection | None, field_name: str) -> Any:
    if correction and getattr(correction, field_name, None) is not None:
        return getattr(correction, field_name)
    return getattr(row, field_name, None)


def get_recent_accepted_corrections(
    db: Session,
    tenant_id: Any,
    company_id: Any | None,
    supplier_key: str,
    field_name: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return accepted field values for a supplier, isolated by tenant/company.

    Company-specific rows are returned first.  Tenant-wide rows from other
    companies are allowed only as weaker evidence and never cross tenants.
    """
    if field_name not in HISTORY_FIELDS or not tenant_id or not supplier_key:
        return []
    q = (
        select(InvoiceRow)
        .where(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.supplier_name.isnot(None),
        )
        .order_by(desc(InvoiceRow.created_at), desc(InvoiceRow.id))
        .limit(500)
    )
    rows = db.execute(q).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        if _similarity(row.supplier_name, supplier_key) < 0.92:
            continue
        corr = db.get(InvoiceRowCorrection, row.id) if row.id else None
        if not _is_row_accepted(db, row, corr):
            continue
        value = _accepted_value_for_field(row, corr, field_name)
        key = _value_key(field_name, value)
        if not key:
            continue
        same_company = bool(company_id and row.company_id == company_id)
        out.append({
            "row_id": row.id,
            "batch_id": row.batch_id,
            "company_id": row.company_id,
            "same_company": same_company,
            "field_name": field_name,
            "value": _display_value(field_name, value),
            "value_key": key,
            "supplier_name": row.supplier_name,
            "accepted_by": "reviewed_correction" if corr and corr.row_reviewed else "exported_or_marked_reviewed",
            "created_at": row.created_at,
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda item: (not item["same_company"], item.get("created_at") or datetime.min), reverse=False)
    return out


def _confidence_from_counts(support_count: int, conflict_count: int, company_specific: bool, field_name: str) -> float:
    if support_count <= 0:
        return 0.0
    if support_count == 1:
        base = 0.48
    elif support_count <= 3:
        base = 0.66
    else:
        base = 0.80
    if company_specific:
        base += 0.04
    if field_name in PATTERN_ONLY_FIELDS:
        # Invoice numbers, dates and amounts are not stable values across invoices.
        base = min(base, 0.56)
    if conflict_count:
        base -= min(0.25, 0.07 * conflict_count)
    return max(0.20, min(0.86, base))


def rank_historical_candidate(
    field_name: str,
    accepted_values: list[dict[str, Any]],
) -> SupplierHistorySignal | None:
    if not accepted_values:
        return None
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in accepted_values:
        key = str(item.get("value_key") or "")
        if key:
            buckets.setdefault(key, []).append(item)
    if not buckets:
        return None

    company_buckets = {
        key: refs for key, refs in buckets.items() if any(ref.get("same_company") for ref in refs)
    }
    usable = company_buckets or buckets
    winning_key, refs = max(usable.items(), key=lambda kv: (len(kv[1]), sum(1 for r in kv[1] if r.get("same_company"))))
    conflict_count = sum(len(v) for k, v in usable.items() if k != winning_key)
    support_count = len(refs)
    company_specific = any(ref.get("same_company") for ref in refs)
    value = refs[0].get("value")
    confidence = _confidence_from_counts(support_count, conflict_count, company_specific, field_name)
    source_type = "accepted_correction" if support_count == 1 else "supplier_history"
    if conflict_count:
        reason = "Historical supplier evidence is conflicting; review required before learning is trusted."
    elif support_count == 1:
        reason = "Single previous accepted correction; suggestion only."
    elif support_count <= 3:
        reason = "Repeated accepted supplier history; medium-confidence learning signal."
    else:
        reason = "Stable repeated accepted supplier history; strong deterministic learning signal."
    return SupplierHistorySignal(
        field_name=field_name,
        value=value,
        confidence=confidence,
        support_count=support_count,
        conflict_count=conflict_count,
        source_type=source_type,
        source_row_id=refs[0].get("row_id"),
        evidence=f"{support_count} accepted row(s), {conflict_count} conflicting accepted value(s), company_specific={company_specific}",
        reason=reason,
        company_specific=company_specific,
        should_apply=(support_count >= 2 and conflict_count == 0 and confidence >= 0.60 and field_name in STABLE_HISTORY_FIELDS),
    )


def calculate_field_success_rate(
    db: Session,
    tenant_id: Any,
    company_id: Any | None,
    supplier_key: str,
    field_name: str,
) -> float | None:
    values = get_recent_accepted_corrections(db, tenant_id, company_id, supplier_key, field_name, limit=200)
    if not values:
        return None
    signal = rank_historical_candidate(field_name, values)
    if not signal:
        return None
    total = signal.support_count + signal.conflict_count
    return round(signal.support_count / total, 4) if total else None


def _rule_history_signals(db: Session, tenant_id: Any, company_id: Any | None, supplier_key: str) -> list[SupplierHistorySignal]:
    signals: list[SupplierHistorySignal] = []
    if not tenant_id or not supplier_key:
        return signals
    rules = db.execute(
        select(CorrectionRule).where(
            CorrectionRule.tenant_id == tenant_id,
            CorrectionRule.active.is_(True),
            CorrectionRule.is_global.is_(False),
            or_(CorrectionRule.company_id == company_id, CorrectionRule.company_id.is_(None)),
        )
    ).scalars().all()
    for rule in rules:
        if rule.field_name not in HISTORY_FIELDS:
            continue
        if _similarity(rule.source_pattern, supplier_key) < 0.92:
            continue
        if not _value_key(rule.field_name, rule.target_value):
            continue
        support = 2 if rule.rule_type in {"supplier_alias", "nominal_remap", "text_correction"} else 1
        conf = 0.68 if support > 1 else 0.52
        signals.append(SupplierHistorySignal(
            field_name=rule.field_name,
            value=_display_value(rule.field_name, rule.target_value),
            confidence=conf,
            support_count=support,
            conflict_count=0,
            source_type="supplier_history",
            evidence=f"accepted active tenant rule id={rule.id}; rule_type={rule.rule_type}",
            reason="Accepted tenant rule contributes supplier-history evidence.",
            company_specific=bool(rule.company_id),
            should_apply=(support >= 2 and rule.field_name in STABLE_HISTORY_FIELDS),
        ))
    return signals


def _remap_history_signals(db: Session, tenant_id: Any, company_id: Any | None, supplier_key: str) -> list[SupplierHistorySignal]:
    signals: list[SupplierHistorySignal] = []
    if not tenant_id or not supplier_key:
        return signals
    hints = db.execute(
        select(RemapHint).where(
            RemapHint.tenant_id == tenant_id,
            RemapHint.active.is_(True),
            or_(RemapHint.company_id == company_id, RemapHint.company_id.is_(None)),
            RemapHint.supplier_name_snapshot.isnot(None),
        )
    ).scalars().all()
    by_field: dict[str, list[RemapHint]] = {}
    for hint in hints:
        if hint.field_name not in HISTORY_FIELDS:
            continue
        if _similarity(hint.supplier_name_snapshot, supplier_key) >= 0.92:
            by_field.setdefault(hint.field_name, []).append(hint)
    for field_name, refs in by_field.items():
        count = len(refs)
        signals.append(SupplierHistorySignal(
            field_name=field_name,
            value="__saved_region_available__",
            confidence=0.60 if count == 1 else 0.70,
            support_count=count,
            conflict_count=0,
            source_type="supplier_history",
            source_row_id=refs[0].source_row_id,
            evidence=f"{count} active saved region(s) for supplier/field",
            reason="Saved remap history confirms this field has supplier-specific extraction guidance.",
            company_specific=any(h.company_id == company_id for h in refs),
            should_apply=False,
        ))
    return signals


def get_supplier_history_profile(
    db: Session,
    tenant_id: Any,
    company_id: Any | None,
    supplier_key: str,
) -> SupplierHistoryProfile:
    key = normalise_supplier_key(supplier_key)
    profile = SupplierHistoryProfile(tenant_id=tenant_id, company_id=company_id, supplier_key=key)
    if not tenant_id or not key:
        return profile

    # Count matched/accepted rows once so the UI/audit can explain scope.
    rows = db.execute(
        select(InvoiceRow).where(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.supplier_name.isnot(None),
        ).order_by(desc(InvoiceRow.created_at)).limit(500)
    ).scalars().all()
    for row in rows:
        if _similarity(row.supplier_name, key) < 0.92:
            continue
        profile.matched_row_count += 1
        corr = db.get(InvoiceRowCorrection, row.id) if row.id else None
        if _is_row_accepted(db, row, corr):
            profile.accepted_row_count += 1
            if company_id and row.company_id == company_id:
                profile.company_accepted_row_count += 1

    for field_name in HISTORY_FIELDS:
        signal = rank_historical_candidate(
            field_name,
            get_recent_accepted_corrections(db, tenant_id, company_id, key, field_name),
        )
        if signal:
            profile.signals.append(signal)
            if signal.conflict_count:
                profile.conflicts[field_name] = signal.conflict_count

    # Include accepted rules and saved-region availability as learning metadata.
    profile.signals.extend(_rule_history_signals(db, tenant_id, company_id, key))
    profile.signals.extend(_remap_history_signals(db, tenant_id, company_id, key))
    return profile

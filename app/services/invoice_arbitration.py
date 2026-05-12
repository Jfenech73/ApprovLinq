"""Deterministic post-extraction arbitration for invoice rows.

This module is intentionally lightweight.  It does not introduce an ML runtime;
instead it ranks explainable candidates from raw extraction, rules, saved-region
activity, supplier history, master data, and totals checks.  The scan pipeline
can keep its existing rule/remap functions, while this layer records why a value
was trusted, applies safe high-confidence improvements, and flags conflicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.db.models import InvoiceBatch, InvoiceRow, TenantNominalAccount, TenantSupplier
from app.db.review_models import CorrectionRule, InvoiceRowCorrection, InvoiceRowFieldAudit, RemapHint
from app.services.validate_invoice import validate_invoice

ARBITRATION_FIELDS: tuple[str, ...] = (
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

SOURCE_RANK = {
    "manual": 100,
    "correction_rule": 90,
    "saved_region": 82,
    "supplier_history": 72,
    "accepted_correction": 70,
    "supplier_master": 68,
    "nominal_master": 67,
    "totals_reconciliation": 65,
    "admin_global_rule": 62,
    "raw_extraction": 50,
}

AMOUNT_FIELDS = {"net_amount", "vat_amount", "total_amount"}


@dataclass
class Candidate:
    field_name: str
    value: Any
    source_type: str
    confidence: float
    evidence: str = ""
    rule_id: int | None = None
    remap_hint_id: int | None = None
    source_row_id: int | None = None
    reason: str = ""
    should_apply: bool = False


@dataclass
class FieldDecision:
    field_name: str
    current_value: Any
    winning_candidate: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    applied: bool = False
    conflict: bool = False
    review_required: bool = False
    reason: str = ""


@dataclass
class ArbitrationResult:
    decisions: dict[str, FieldDecision] = field(default_factory=dict)
    review_required: bool = False
    method_tags: list[str] = field(default_factory=list)
    review_fields: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _norm_supplier(value: Any) -> str:
    text = _norm_text(value)
    text = re.sub(
        r"\b(ltd|limited|plc|llc|inc|corp|co|company|group|trading|holdings|services|solutions)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: Any, b: Any) -> float:
    aa, bb = _norm_supplier(a), _norm_supplier(b)
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
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"))
    try:
        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(Decimal("0.01"))
        text = str(value).strip()
        text = re.sub(r"[^0-9,\.\-]", "", text)
        if not text or text in {"-", ".", ","}:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text and "." not in text:
            text = text.replace(",", ".")
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _as_string_for_audit(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _append_unique(existing: str | None, token: str, sep: str = "|") -> str:
    token = (token or "").strip()
    if not token:
        return existing or ""
    parts = [p.strip() for p in re.split(r"[|,]", existing or "") if p.strip()]
    if token not in parts:
        parts.append(token)
    return sep.join(parts)[:500]


def _append_method(row: InvoiceRow, tag: str) -> None:
    tag = (tag or "").strip()
    if not tag:
        return
    parts = [p.strip() for p in re.split(r"[+|,]", row.method_used or "") if p.strip()]
    if tag not in parts:
        parts.append(tag)
    row.method_used = "+".join(parts)[:200]


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _invoice_number_suspicious(value: Any, supplier_name: Any = None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if len(text) < 3:
        return True
    if _parse_date(text):
        return True
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8 and re.fullmatch(r"[\d\s+\-()]+", text):
        return True  # phone/VAT-like number
    if supplier_name and _similarity(text, supplier_name) >= 0.85:
        return True
    if re.search(r"\b(vat|tax|tel|phone|mob)\b", text, re.I):
        return True
    return False


def _value_valid_for_field(field_name: str, value: Any, row: InvoiceRow | None = None) -> bool:
    if value is None or value == "":
        return False
    if field_name in AMOUNT_FIELDS:
        return _money(value) is not None
    if field_name == "invoice_date":
        d = _parse_date(value)
        if not d:
            return False
        today = date.today()
        return today - timedelta(days=3650) <= d <= today + timedelta(days=370)
    if field_name == "invoice_number":
        return not _invoice_number_suspicious(value, getattr(row, "supplier_name", None) if row else None)
    if field_name == "supplier_name":
        text = str(value or "").strip()
        return len(text) >= 3 and not re.fullmatch(r"[0-9\-/. ]+", text)
    if field_name == "currency":
        return bool(re.fullmatch(r"[A-Za-z]{3}|€|eur|usd|gbp", str(value).strip(), re.I))
    return bool(str(value).strip())


def _normalise_field_value(field_name: str, value: Any) -> Any:
    if field_name in AMOUNT_FIELDS:
        m = _money(value)
        return float(m) if m is not None else value
    if field_name == "invoice_date":
        return _parse_date(value) or value
    if field_name == "currency":
        text = str(value or "").strip().upper()
        if text == "€":
            return "EUR"
        return text[:3] if len(text) >= 3 else text
    return str(value).strip() if value is not None else None


def _values_equivalent(field_name: str, a: Any, b: Any) -> bool:
    if a is None or a == "" or b is None or b == "":
        return False
    if field_name in AMOUNT_FIELDS:
        ma, mb = _money(a), _money(b)
        return ma is not None and mb is not None and abs(ma - mb) <= Decimal("0.02")
    if field_name == "invoice_date":
        return _parse_date(a) == _parse_date(b)
    if field_name == "supplier_name":
        return _similarity(a, b) >= 0.92
    return _norm_text(a) == _norm_text(b)


def _current_value_is_weak(row: InvoiceRow, field_name: str) -> bool:
    value = getattr(row, field_name, None)
    if value is None or str(value).strip() == "":
        return True
    review_fields = {f.strip() for f in re.split(r"[|,]", row.review_fields or "") if f.strip()}
    if field_name in review_fields:
        return True
    try:
        conf = float(row.confidence_score) if row.confidence_score is not None else None
    except Exception:
        conf = None
    if row.review_required and (conf is None or conf < 0.70):
        return True
    if conf is not None and conf < 0.55:
        return True
    if not _value_valid_for_field(field_name, value, row):
        return True
    return False


def _audit_arbitration(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    field_name: str,
    old_value: Any,
    new_value: Any,
    action: str,
    note: str,
) -> None:
    if row.id is None:
        db.add(row)
        db.flush()
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name=field_name,
        old_value=_as_string_for_audit(old_value),
        new_value=_as_string_for_audit(new_value),
        action=action[:40],
        note=note[:1000],
        rule_created=False,
        user_id=None,
        username="system",
    ))


def _raw_candidates(row: InvoiceRow, extraction_payload: dict[str, Any] | None) -> list[Candidate]:
    payload = extraction_payload or {}
    out: list[Candidate] = []
    try:
        base_conf = float(payload.get("confidence_score") or row.confidence_score or 0.50)
    except Exception:
        base_conf = 0.50
    for field_name in ARBITRATION_FIELDS:
        value = payload.get(field_name)
        if value is None:
            value = getattr(row, field_name, None)
        if _value_valid_for_field(field_name, value, row):
            out.append(Candidate(
                field_name=field_name,
                value=_normalise_field_value(field_name, value),
                source_type="raw_extraction",
                confidence=max(0.20, min(base_conf, 0.95)),
                evidence=str(payload.get("method_used") or row.method_used or "raw extraction"),
                reason="Raw extraction candidate validated by field type.",
            ))
    return out


def _rule_candidates(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> list[Candidate]:
    candidates: list[Candidate] = []
    row_supplier = _norm_supplier(row.supplier_name)
    q = db.query(CorrectionRule).filter(
        CorrectionRule.active.is_(True),
        or_(CorrectionRule.tenant_id == batch.tenant_id, CorrectionRule.is_global.is_(True)),
    )
    if batch.company_id:
        q = q.filter(or_(CorrectionRule.company_id == batch.company_id, CorrectionRule.company_id.is_(None), CorrectionRule.is_global.is_(True)))
    else:
        q = q.filter(or_(CorrectionRule.company_id.is_(None), CorrectionRule.is_global.is_(True)))
    for rule in q.order_by(CorrectionRule.is_global.asc(), desc(CorrectionRule.id)).all():
        field_name = rule.field_name or ""
        if field_name not in ARBITRATION_FIELDS:
            continue
        value = rule.target_value
        if not _value_valid_for_field(field_name, value, row):
            continue
        source_norm = _norm_supplier(rule.source_pattern)
        current_field_norm = _norm_supplier(getattr(row, field_name, None))
        supplier_match = bool(row_supplier and source_norm and row_supplier == source_norm)
        field_match = bool(current_field_norm and source_norm and current_field_norm == source_norm)
        if rule.rule_type in {"remap_field_value", "supplier_alias"} and not supplier_match:
            continue
        if rule.rule_type == "text_correction" and not (supplier_match or field_match):
            continue
        if rule.rule_type == "nominal_remap" and field_name == "nominal_account_code" and not field_match:
            continue
        candidates.append(Candidate(
            field_name=field_name,
            value=_normalise_field_value(field_name, value),
            source_type="admin_global_rule" if getattr(rule, "is_global", False) else "correction_rule",
            confidence=0.88 if not getattr(rule, "is_global", False) else 0.76,
            evidence=f"rule_type={rule.rule_type}; source_pattern={rule.source_pattern}",
            rule_id=rule.id,
            reason="Active correction rule matched current supplier or field value.",
        ))
    return candidates


def _audit_candidates(db: Session, row: InvoiceRow) -> list[Candidate]:
    if row.id is None:
        return []
    out: list[Candidate] = []
    audits = db.execute(
        select(InvoiceRowFieldAudit)
        .where(InvoiceRowFieldAudit.row_id == row.id)
        .order_by(desc(InvoiceRowFieldAudit.created_at))
    ).scalars().all()
    for audit in audits:
        field_name = audit.field_name
        if field_name not in ARBITRATION_FIELDS:
            continue
        value = audit.new_value
        if not _value_valid_for_field(field_name, value, row):
            continue
        action = (audit.action or "").lower()
        note = audit.note or ""
        source = "saved_region" if "saved_region" in action or "remap_hint" in note else "correction_rule"
        m = re.search(r"remap_hint_id=(\d+)", note)
        rid = re.search(r"rule_id=(\d+)", note)
        out.append(Candidate(
            field_name=field_name,
            value=_normalise_field_value(field_name, value),
            source_type=source,
            confidence=0.84 if source == "saved_region" else 0.86,
            evidence=note or action,
            remap_hint_id=int(m.group(1)) if m else None,
            rule_id=int(rid.group(1)) if rid else None,
            reason="Candidate produced by automatic rule/saved-region action earlier in this scan.",
        ))
    return out


def _supplier_master_candidates(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> list[Candidate]:
    if not row.supplier_name or not batch.tenant_id or not batch.company_id:
        return []
    out: list[Candidate] = []
    suppliers = db.execute(
        select(TenantSupplier).where(
            TenantSupplier.tenant_id == batch.tenant_id,
            TenantSupplier.company_id == batch.company_id,
            TenantSupplier.is_active.is_(True),
        )
    ).scalars().all()
    best = None
    best_score = 0.0
    for supp in suppliers:
        score = _similarity(row.supplier_name, supp.supplier_name)
        if score > best_score:
            best, best_score = supp, score
    if best and best_score >= 0.86:
        out.append(Candidate(
            field_name="supplier_name",
            value=best.supplier_name,
            source_type="supplier_master",
            confidence=min(0.92, best_score),
            evidence=f"tenant supplier master match score={best_score:.2f}",
            reason="Supplier matched tenant/company supplier master list.",
        ))
        if best.default_nominal and _value_valid_for_field("nominal_account_code", best.default_nominal, row):
            out.append(Candidate(
                field_name="nominal_account_code",
                value=best.default_nominal,
                source_type="supplier_master",
                confidence=0.78,
                evidence="tenant supplier default nominal",
                reason="Default nominal account from matched supplier master record.",
            ))
    return out


def _nominal_master_candidates(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> list[Candidate]:
    value = row.nominal_account_code
    if not value or not batch.tenant_id or not batch.company_id:
        return []
    account = db.execute(
        select(TenantNominalAccount).where(
            TenantNominalAccount.tenant_id == batch.tenant_id,
            TenantNominalAccount.company_id == batch.company_id,
            TenantNominalAccount.account_code == str(value).strip(),
            TenantNominalAccount.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not account:
        return []
    return [Candidate(
        field_name="nominal_account_code",
        value=account.account_code,
        source_type="nominal_master",
        confidence=0.86,
        evidence=f"tenant nominal master: {account.account_name}",
        reason="Nominal account exists in tenant/company nominal master list.",
    )]


def _history_candidates(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> list[Candidate]:
    supplier_norm = _norm_supplier(row.supplier_name)
    if not supplier_norm or not batch.tenant_id:
        return []
    rows = db.execute(
        select(InvoiceRow)
        .where(
            InvoiceRow.tenant_id == batch.tenant_id,
            InvoiceRow.id != (row.id or -1),
            InvoiceRow.supplier_name.isnot(None),
        )
        .order_by(desc(InvoiceRow.created_at))
        .limit(300)
    ).scalars().all()
    out: list[Candidate] = []
    support: dict[str, dict[str, list[InvoiceRow]]] = {f: {} for f in ARBITRATION_FIELDS}
    for prev in rows:
        if batch.company_id and prev.company_id not in {batch.company_id, None}:
            continue
        if _similarity(prev.supplier_name, row.supplier_name) < 0.92:
            continue
        corr = db.get(InvoiceRowCorrection, prev.id)
        accepted = bool(corr and corr.row_reviewed) or bool(prev.auto_approved) or str(prev.validation_status or "").lower() in {"ok", "valid", "passed"}
        if not accepted:
            continue
        for field_name in ARBITRATION_FIELDS:
            value = getattr(corr, field_name, None) if corr and getattr(corr, field_name, None) is not None else getattr(prev, field_name, None)
            if not _value_valid_for_field(field_name, value, row):
                continue
            key = _norm_supplier(value) if field_name == "supplier_name" else str(_normalise_field_value(field_name, value))
            support[field_name].setdefault(key, []).append(prev)
    for field_name, buckets in support.items():
        if not buckets:
            continue
        key, refs = max(buckets.items(), key=lambda kv: len(kv[1]))
        count = len(refs)
        # One prior correction should be visible but not strong enough to apply.
        if count <= 1:
            conf = 0.48
        elif count <= 3:
            conf = 0.66
        else:
            conf = 0.78
        example = getattr(refs[0], field_name, None)
        corr = db.get(InvoiceRowCorrection, refs[0].id)
        if corr and getattr(corr, field_name, None) is not None:
            example = getattr(corr, field_name)
        out.append(Candidate(
            field_name=field_name,
            value=_normalise_field_value(field_name, example),
            source_type="supplier_history" if count > 1 else "accepted_correction",
            confidence=conf,
            evidence=f"{count} accepted previous row(s) for same supplier",
            source_row_id=refs[0].id,
            reason="Repeated accepted supplier history." if count > 1 else "Single previous accepted correction; suggestion only.",
        ))
    return out


def _totals_candidates(row: InvoiceRow, extraction_payload: dict[str, Any] | None) -> list[Candidate]:
    out: list[Candidate] = []
    net, vat, total = _money(row.net_amount), _money(row.vat_amount), _money(row.total_amount)
    if net is not None and vat is not None:
        expected = (net + vat).quantize(Decimal("0.01"))
        if total is None:
            out.append(Candidate(
                field_name="total_amount",
                value=float(expected),
                source_type="totals_reconciliation",
                confidence=0.72,
                evidence="net + vat calculated total",
                reason="Total was blank but net and VAT were available.",
            ))
        elif abs(expected - total) <= Decimal("0.03"):
            for field_name, value in (("net_amount", net), ("vat_amount", vat), ("total_amount", total)):
                out.append(Candidate(
                    field_name=field_name,
                    value=float(value),
                    source_type="totals_reconciliation",
                    confidence=0.82,
                    evidence="net + vat reconciles to total",
                    reason="Amount is supported by totals reconciliation.",
                ))
    try:
        validation = validate_invoice({
            "net_amount": float(net) if net is not None else None,
            "vat_amount": float(vat) if vat is not None else None,
            "total_amount": float(total) if total is not None else None,
            "raw_text": "\n".join(str((extraction_payload or {}).get(k) or "") for k in ("page_text_raw", "totals_raw", "header_raw")),
        })
        if getattr(validation, "status", None):
            row.totals_reconciliation_status = row.totals_reconciliation_status or str(validation.status)
    except Exception:
        pass
    return out


def _choose_candidate(field_name: str, candidates: list[Candidate]) -> tuple[Candidate | None, bool, str]:
    valid = [c for c in candidates if _value_valid_for_field(field_name, c.value)]
    if not valid:
        return None, False, "No valid candidates."
    valid.sort(key=lambda c: (SOURCE_RANK.get(c.source_type, 0), c.confidence), reverse=True)
    winner = valid[0]
    strong = [c for c in valid if c.confidence >= 0.78 or SOURCE_RANK.get(c.source_type, 0) >= 80]
    conflicts = [c for c in strong if not _values_equivalent(field_name, c.value, winner.value)]
    if conflicts:
        other = conflicts[0]
        return winner, True, f"Strong candidate conflict: {winner.source_type}={winner.value!r} vs {other.source_type}={other.value!r}."
    return winner, False, winner.reason or "Highest ranked valid candidate selected."


def arbitrate_invoice_row(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    extraction_payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> ArbitrationResult:
    """Rank evidence sources and safely apply/flag field decisions for a row."""
    result = ArbitrationResult()
    candidates: list[Candidate] = []
    candidates.extend(_raw_candidates(row, extraction_payload))
    candidates.extend(_audit_candidates(db, row))
    candidates.extend(_rule_candidates(db, batch, row))
    candidates.extend(_supplier_master_candidates(db, batch, row))
    candidates.extend(_nominal_master_candidates(db, batch, row))
    candidates.extend(_history_candidates(db, batch, row))
    candidates.extend(_totals_candidates(row, extraction_payload))

    by_field: dict[str, list[Candidate]] = {f: [] for f in ARBITRATION_FIELDS}
    for cand in candidates:
        if cand.field_name in by_field and _value_valid_for_field(cand.field_name, cand.value, row):
            by_field[cand.field_name].append(cand)

    for field_name in ARBITRATION_FIELDS:
        current = getattr(row, field_name, None)
        field_candidates = by_field.get(field_name, [])
        winner, conflict, reason = _choose_candidate(field_name, field_candidates)
        decision = FieldDecision(
            field_name=field_name,
            current_value=current,
            winning_candidate=winner,
            candidates=field_candidates,
            conflict=conflict,
            reason=reason,
        )
        result.decisions[field_name] = decision
        if not winner:
            if field_name in {"supplier_name", "invoice_number", "invoice_date", "total_amount"} and _current_value_is_weak(row, field_name):
                decision.review_required = True
                result.review_required = True
                result.review_fields.append(field_name)
                result.review_reasons.append(f"arbitration_no_valid_candidate:{field_name}")
            continue

        weak_current = _current_value_is_weak(row, field_name)
        same = _values_equivalent(field_name, current, winner.value)
        if conflict:
            decision.review_required = True
            result.review_required = True
            result.review_fields.append(field_name)
            result.review_reasons.append(f"arbitration_conflict:{field_name}")
            _append_method(row, f"arbitration_conflict:{field_name}")
            _audit_arbitration(
                db, batch, row, field_name, current, current,
                "arbitration_conflict",
                f"{reason}; winner_source={winner.source_type}; evidence={winner.evidence}",
            )
            continue

        # Stronger sources can fill weak fields.  A strong existing value is only
        # overwritten when the candidate comes from manual/rule/saved-region or a
        # tenant master exact/fuzzy match.  Supplier history with one prior row is
        # deliberately suggestion-only.
        source_rank = SOURCE_RANK.get(winner.source_type, 0)
        can_apply = False
        if same:
            can_apply = False
            _append_method(row, f"arbitrated:{winner.source_type}:{field_name}")
        elif weak_current and winner.confidence >= 0.60:
            can_apply = True
        elif source_rank >= 80 and winner.confidence >= 0.78:
            can_apply = True
        elif winner.source_type in {"supplier_master", "nominal_master"} and winner.confidence >= 0.86:
            can_apply = True

        # One-off history/correction suggestions must not auto-apply.
        if winner.source_type == "accepted_correction" and winner.confidence < 0.60:
            can_apply = False

        if can_apply:
            new_value = _normalise_field_value(field_name, winner.value)
            old_value = current
            setattr(row, field_name, new_value)
            decision.applied = True
            winner.should_apply = True
            _append_method(row, f"arbitrated:{winner.source_type}:{field_name}")
            _audit_arbitration(
                db, batch, row, field_name, old_value, new_value,
                "arbitration_apply",
                f"Applied {winner.source_type} candidate confidence={winner.confidence:.2f}; {winner.reason}; evidence={winner.evidence}",
            )
        elif weak_current and winner.confidence < 0.60:
            decision.review_required = True
            result.review_required = True
            result.review_fields.append(field_name)
            result.review_reasons.append(f"arbitration_weak_candidate:{field_name}")
        elif not same and source_rank >= 70 and winner.confidence >= 0.70:
            # Good suggestion but not enough evidence to overwrite a strong value.
            decision.review_required = True
            result.review_required = True
            result.review_fields.append(field_name)
            result.review_reasons.append(f"arbitration_suggestion:{field_name}")
            _audit_arbitration(
                db, batch, row, field_name, current, winner.value,
                "arbitration_suggest",
                f"Suggested {winner.source_type} candidate confidence={winner.confidence:.2f}; not applied because current value is strong; evidence={winner.evidence}",
            )

    if result.review_required:
        row.review_required = True
        for field_name in result.review_fields:
            row.review_fields = _append_unique(row.review_fields, field_name)
        for reason in result.review_reasons:
            row.review_reasons = _append_unique(row.review_reasons, reason)
    return result

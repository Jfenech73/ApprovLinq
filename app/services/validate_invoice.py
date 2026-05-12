"""Invoice arithmetic and business-rule validation.

Validates financial fields extracted from an invoice:
  - Line sum vs net (when line items are present)
  - net + vat == total (within tolerance)
  - VAT rate plausibility
  - Deposit / discount / other-charge component detection

Public API
──────────
    validate_invoice(extracted: dict) -> InvoiceValidation

InvoiceValidation fields:
    net_amount
    vat_amount
    deposit_amount           - detected deposit/BCRS surcharge (if any)
    discount_amount          - detected discount (negative adjustment, if any)
    other_charges_amount     - any other unexplained adjustment
    total_amount
    totals_reconciliation_status  - "ok" | "ok_with_deposit" | "ok_with_discount"
                                    | "vat_missing" | "vat_anomaly"
                                    | "totals_mismatch" | "line_sum_mismatch"
                                    | "subtotal_not_found"
    totals_reconciliation_reason  - human-readable explanation
    review_reasons           - list of reason codes raised by validation
    vat_rate                 - computed VAT rate (vat / net) or None
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.totals_reconciliation import reconcile_invoice_totals


@dataclass
class InvoiceValidation:
    net_amount: Optional[float] = None
    vat_amount: Optional[float] = None
    deposit_amount: Optional[float] = None
    discount_amount: Optional[float] = None
    other_charges_amount: Optional[float] = None
    total_amount: Optional[float] = None
    totals_reconciliation_status: str = "ok"
    totals_reconciliation_reason: str = ""
    review_reasons: list[str] = field(default_factory=list)
    vat_rate: Optional[float] = None


def _round2(v: float) -> float:
    return round(v, 2)


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Deposit detection ──────────────────────────────────────────────────────────

_DEPOSIT_DENOMINATIONS = {0.0, 0.10, 0.20, 0.25, 0.40, 0.50, 0.60, 0.75, 0.80}


def _is_deposit_amount(diff: float, net: float | None = None) -> bool:
    """True if |diff| looks like a deposit/BCRS surcharge or returnables charge.

    Heuristic:
        - Positive (surcharge, not a discount)
        - ≤ €200 (large batch deliveries can carry substantial BCRS totals,
          e.g. J. Sultana-style invoices with 70.80 for many glass-bottle units)
        - Centavos part is one of the common BCRS denomination remainders
          (0c, 10c, 20c, 25c, 40c, 50c, 60c, 75c, 80c — per-unit multiples)
        - Must be ≤ 40% of net when net is available — a diff that is more than
          40% of the commercial value is clearly not a deposit/BCRS surcharge
          (e.g. net=100, total=200 → diff=77 is 77% of net → totals_mismatch)
    """
    if not (0.01 <= diff <= 200.00):
        return False
    # Additional proportionality guard: deposit cannot be a huge share of net
    if net is not None and net > 0 and (diff / net) > 0.40:
        return False
    frac = round(diff % 1.0, 2)
    return frac in _DEPOSIT_DENOMINATIONS


def _is_discount_amount(diff: float) -> bool:
    """True if the difference is a plausible discount (negative adjustment, ≤ 10%)."""
    return -500.00 <= diff < -0.01


# ── Main validation ────────────────────────────────────────────────────────────

def validate_invoice(extracted: dict) -> InvoiceValidation:
    """Validate the financial fields of an extracted invoice.

    This compatibility wrapper now delegates totals arithmetic to
    totals_reconciliation.reconcile_invoice_totals while preserving the historic
    InvoiceValidation return shape used by extractor/review code.
    """
    net = _safe_float(extracted.get("source_invoice_net_amount") or extracted.get("net_amount"))
    vat = _safe_float(extracted.get("source_invoice_vat_amount") or extracted.get("vat_amount"))
    total = _safe_float(extracted.get("source_invoice_total_amount") or extracted.get("total_amount"))

    result = InvoiceValidation(net_amount=net, vat_amount=vat, total_amount=total)
    reasons: list[str] = []

    # VAT rate plausibility remains here for backward-compatible reason codes.
    if net is not None and net > 0:
        if vat is not None:
            vat_rate = round(vat / net, 4)
            result.vat_rate = vat_rate
            if vat_rate > 0.35:
                reasons.append("vat_anomaly")
                result.totals_reconciliation_status = "vat_anomaly"
                result.totals_reconciliation_reason = (
                    f"VAT rate {vat_rate:.1%} exceeds 35% — likely extraction error"
                )
            elif 0 < vat_rate < 0.015:
                reasons.append("vat_anomaly")
                result.totals_reconciliation_status = "vat_anomaly"
                result.totals_reconciliation_reason = (
                    f"VAT rate {vat_rate:.1%} below 1.5% — unusually low"
                )
        elif total is not None and total > net * 1.02:
            reasons.append("vat_missing")
            result.totals_reconciliation_status = "vat_missing"
            result.totals_reconciliation_reason = (
                "Total exceeds net by >2% but no VAT amount was extracted"
            )

    rec = reconcile_invoice_totals(extracted, extracted, line_items=extracted.get("line_items") or extracted.get("items"))
    result.deposit_amount = rec.bcrs_amount
    result.discount_amount = rec.discount_amount
    result.total_amount = rec.total_amount
    result.net_amount = rec.net_amount
    result.vat_amount = rec.vat_amount

    status_map = {
        "reconciled": "ok",
        "line_items_reconciled": "ok",
        "reconciled_with_bcrs": "ok_with_deposit",
        "reconciled_with_discount": "ok_with_discount",
        "reconciled_with_bcrs_and_discount": "ok_with_deposit_and_discount",
        "line_items_mismatch": "line_sum_mismatch",
        "mismatch_requires_review": "totals_mismatch",
        "subtotal_not_found": "subtotal_not_found",
        "insufficient_data": "subtotal_not_found",
    }
    # Preserve VAT anomaly/missing status if already raised; otherwise use totals status.
    if result.totals_reconciliation_status == "ok":
        result.totals_reconciliation_status = status_map.get(rec.status, rec.status)
        result.totals_reconciliation_reason = rec.review_reason or "; ".join(rec.evidence) or rec.status
    elif rec.review_required and "totals_mismatch" not in reasons:
        # VAT anomaly plus totals mismatch should still surface both conditions.
        reasons.append("totals_mismatch")

    if rec.bcrs_amount:
        # Advisory only.  batches.py still requires label/region evidence before split.
        reasons.append(f"deposit_component_detected:{rec.bcrs_amount:.2f}")
        if rec.status not in {"reconciled_with_bcrs", "reconciled_with_bcrs_and_discount"}:
            reasons.append("totals_mismatch_advisory")
    if rec.discount_amount:
        result.discount_amount = rec.discount_amount
    if rec.status in {"mismatch_requires_review", "line_items_mismatch"}:
        reasons.append("line_sum_mismatch" if rec.status == "line_items_mismatch" else "totals_mismatch")
    if rec.status in {"subtotal_not_found", "insufficient_data"} and net is None and total is None:
        reasons.append("subtotal_not_found")

    # de-duplicate while preserving order
    seen = set()
    result.review_reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return result

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


def _is_deposit_amount(diff: float) -> bool:
    """True if |diff| looks like a deposit/BCRS surcharge or returnables charge.

    Heuristic:
        - Positive (surcharge, not a discount)
        - ≤ €200 (large batch deliveries can carry substantial BCRS totals,
          e.g. J. Sultana-style invoices with 70.80 for many glass-bottle units)
        - Centavos part is one of the common BCRS denomination remainders
          (0c, 10c, 20c, 25c, 40c, 50c, 60c, 75c, 80c — per-unit multiples)
    """
    if not (0.01 <= diff <= 200.00):
        return False
    frac = round(diff % 1.0, 2)
    return frac in _DEPOSIT_DENOMINATIONS


def _is_discount_amount(diff: float) -> bool:
    """True if the difference is a plausible discount (negative adjustment, ≤ 10%)."""
    return -500.00 <= diff < -0.01


# ── Main validation ────────────────────────────────────────────────────────────

def validate_invoice(extracted: dict) -> InvoiceValidation:
    """Validate the financial fields of an extracted invoice.

    Args:
        extracted: dict from process_pdf_page (or any stage of the pipeline).

    Returns:
        InvoiceValidation with reconciliation status and reason codes.
    """
    net   = _safe_float(extracted.get("net_amount"))
    vat   = _safe_float(extracted.get("vat_amount"))
    total = _safe_float(extracted.get("total_amount"))

    result = InvoiceValidation(
        net_amount=net,
        vat_amount=vat,
        total_amount=total,
    )

    reasons: list[str] = []

    # ── VAT rate check ────────────────────────────────────────────────────────
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

    # ── Total reconciliation ──────────────────────────────────────────────────
    if net is not None and total is not None:
        vat_val = vat or 0.0
        expected = _round2(net + vat_val)
        diff = _round2(total - expected)
        tolerance = 0.10

        if abs(diff) <= tolerance:
            # Within tolerance — reconciled
            if result.totals_reconciliation_status == "ok":
                result.totals_reconciliation_status = "ok"
                result.totals_reconciliation_reason = (
                    f"Reconciled: net({net:.2f}) + vat({vat_val:.2f}) = total({total:.2f})"
                )
        elif diff > tolerance:
            # Total is higher than expected
            if _is_deposit_amount(diff):
                result.deposit_amount = diff
                result.totals_reconciliation_status = "ok_with_deposit"
                result.totals_reconciliation_reason = (
                    f"Difference of {diff:.2f} attributed to deposit/BCRS surcharge"
                )
                # NOTE: deposit_component_detected is purely advisory metadata.
                # It is intentionally NOT used as the basis for an automatic split in
                # batches.py — that requires confirmed label+region evidence.
                # Here it only flags the row for review so a human can verify.
                reasons.append(f"deposit_component_detected:{diff:.2f}")
                reasons.append("totals_mismatch_advisory")
            else:
                result.other_charges_amount = diff
                result.totals_reconciliation_status = "totals_mismatch"
                result.totals_reconciliation_reason = (
                    f"Total({total:.2f}) exceeds net+vat({expected:.2f}) "
                    f"by {diff:.2f} — unexplained surcharge"
                )
                reasons.append("totals_mismatch")
        else:
            # Total is lower than expected
            if _is_discount_amount(diff):
                result.discount_amount = abs(diff)
                result.totals_reconciliation_status = "ok_with_discount"
                result.totals_reconciliation_reason = (
                    f"Difference of {abs(diff):.2f} attributed to discount"
                )
            else:
                result.totals_reconciliation_status = "totals_mismatch"
                result.totals_reconciliation_reason = (
                    f"Total({total:.2f}) is less than net+vat({expected:.2f}) "
                    f"by {abs(diff):.2f} — possible extraction error"
                )
                reasons.append("totals_mismatch")

    elif total is None and net is None:
        reasons.append("subtotal_not_found")
        result.totals_reconciliation_status = "subtotal_not_found"
        result.totals_reconciliation_reason = "Neither net nor total amount could be extracted"

    result.review_reasons = reasons
    return result

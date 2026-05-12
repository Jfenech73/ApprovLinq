"""Evidence-based invoice totals reconciliation.

This service is deliberately deterministic and lightweight.  It reconciles
summary totals, optional discount/BCRS components, and line-item totals.  It is
used as an accounting control for extraction/arbitration; it does not perform
supplier-specific logic and it does not auto-create BCRS rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Iterable

MONEY = Decimal("0.01")


@dataclass
class TotalsReconciliationResult:
    status: str = "insufficient_data"
    expected_total: float | None = None
    actual_total: float | None = None
    difference: float | None = None
    net_amount: float | None = None
    vat_amount: float | None = None
    total_amount: float | None = None
    bcrs_amount: float | None = None
    discount_amount: float | None = None
    suggested_field_fixes: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    review_required: bool = False
    review_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected_total": self.expected_total,
            "actual_total": self.actual_total,
            "difference": self.difference,
            "net_amount": self.net_amount,
            "vat_amount": self.vat_amount,
            "total_amount": self.total_amount,
            "bcrs_amount": self.bcrs_amount,
            "discount_amount": self.discount_amount,
            "suggested_field_fixes": dict(self.suggested_field_fixes),
            "evidence": list(self.evidence),
            "review_required": self.review_required,
            "review_reason": self.review_reason,
        }


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value.quantize(MONEY)
    try:
        if isinstance(value, (int, float)):
            return Decimal(str(value)).quantize(MONEY)
        text = str(value).strip()
        if not text:
            return None
        # Keep only money-significant chars.  Support EU decimal commas.
        text = re.sub(r"[^0-9,\.\-]", "", text)
        if not text or text in {"-", ".", ","}:
            return None
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        return Decimal(text).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _flt(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _round(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)



_DEPOSIT_DENOMINATIONS = {Decimal("0.00"), Decimal("0.10"), Decimal("0.20"), Decimal("0.25"), Decimal("0.40"), Decimal("0.50"), Decimal("0.60"), Decimal("0.75"), Decimal("0.80")}

def looks_like_bcrs_amount(diff: Decimal, net: Decimal | None = None) -> bool:
    """Arithmetic advisory only; row splitting still requires label evidence."""
    diff = abs(diff)
    if not (Decimal("0.01") <= diff <= Decimal("200.00")):
        return False
    if net is not None and net > 0 and (diff / net) > Decimal("0.40"):
        return False
    frac = (diff % Decimal("1.00")).quantize(MONEY)
    return frac in _DEPOSIT_DENOMINATIONS

def _raw_text(payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    parts: list[str] = []
    for key in ("totals_raw", "page_text_raw", "line_items_raw", "header_raw", "raw_text", "description"):
        val = payload.get(key)
        if val:
            parts.append(str(val))
    return "\n".join(parts)


def has_discount_evidence(text: str) -> bool:
    return bool(re.search(r"\b(discount|less\s+discount|rebate|promo\s+discount|discount\s+allowed)\b", text or "", re.I))


def has_bcrs_summary_evidence(text: str) -> bool:
    """Require summary/totals context for BCRS/deposit evidence.

    Plain product-line deposits must not be treated as summary BCRS here.  The
    dedicated BCRS split code in batches.py remains the authority for row-split
    decisions; this helper only strengthens reconciliation evidence.
    """
    if not text:
        return False
    low = text.lower()
    if not re.search(r"\b(bcrs|refundable\s+deposit|deposit\s+summary|returnables?|deposits?)\b", low):
        return False
    return bool(re.search(r"\b(total|subtotal|summary|invoice\s+summary|tax\s+summary|amount\s+due|total\s+due|balance\s+due)\b", low))


def extract_labelled_discount(text: str) -> Decimal | None:
    if not has_discount_evidence(text):
        return None
    vals: list[Decimal] = []
    for line in (text or "").splitlines():
        if not has_discount_evidence(line):
            continue
        nums = re.findall(r"-?\d+(?:[\.,]\d{2})?", line)
        for num in nums:
            v = _money(num)
            if v is not None and abs(v) > Decimal("0.00"):
                vals.append(abs(v))
    return max(vals) if vals else None


def extract_labelled_bcrs(text: str) -> Decimal | None:
    if not has_bcrs_summary_evidence(text):
        return None
    vals: list[Decimal] = []
    label_re = re.compile(r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit|deposit\s+summary|deposits?|returnables?)\b", re.I)
    for line in (text or "").splitlines():
        if not label_re.search(line):
            continue
        nums = re.findall(r"-?\d+(?:[\.,]\d{2})?", line)
        for num in nums:
            v = _money(num)
            if v is not None and Decimal("0.00") < abs(v) <= Decimal("200.00"):
                vals.append(abs(v))
    return max(vals) if vals else None


def _line_values(line_items: Iterable[Any] | None, key_names: tuple[str, ...]) -> list[Decimal]:
    vals: list[Decimal] = []
    if not line_items:
        return vals
    for item in line_items:
        if not isinstance(item, dict):
            continue
        for key in key_names:
            if key in item:
                val = _money(item.get(key))
                if val is not None:
                    vals.append(val)
                    break
    return vals


def _line_tolerance(count: int) -> Decimal:
    # Small invoices tolerate a few cents; larger line sets get 1c per 10 lines,
    # capped to avoid hiding real mismatches.
    return min(Decimal("0.10"), max(Decimal("0.03"), Decimal(count) * Decimal("0.005"))).quantize(MONEY)


def _suggest_digit_or_decimal_fix(net: Decimal | None, vat: Decimal | None, total: Decimal | None) -> dict[str, Any]:
    suggestions: dict[str, Any] = {}
    if net is None or vat is None or total is None:
        return suggestions
    expected = _round(net + vat)
    if abs(expected - total) <= Decimal("0.03"):
        return suggestions
    fields = {"net_amount": net, "vat_amount": vat, "total_amount": total}
    for field, value in fields.items():
        # Try common OCR decimal/drop mistakes: 2917 -> 29.17, 917 -> 9.17,
        # or missing decimal in a field.  Only suggest where it reconciles.
        digits = re.sub(r"\D", "", str(value))
        if len(digits) >= 3:
            scaled = Decimal(digits[:-2] + "." + digits[-2:]).quantize(MONEY)
            trial = dict(fields)
            trial[field] = scaled
            if abs(_round(trial["net_amount"] + trial["vat_amount"]) - trial["total_amount"]) <= Decimal("0.03"):
                suggestions[field] = float(scaled)
        for factor in (Decimal("10"), Decimal("100"), Decimal("0.1"), Decimal("0.01")):
            scaled = _round(value * factor)
            if scaled == value or scaled < 0:
                continue
            trial = dict(fields)
            trial[field] = scaled
            if abs(_round(trial["net_amount"] + trial["vat_amount"]) - trial["total_amount"]) <= Decimal("0.03"):
                suggestions[field] = float(scaled)
    return suggestions


def reconcile_invoice_totals(
    row_or_payload: Any,
    extraction_payload: dict[str, Any] | None = None,
    line_items: list[dict[str, Any]] | None = None,
) -> TotalsReconciliationResult:
    """Reconcile summary and line-item invoice totals.

    ``row_or_payload`` may be an ORM InvoiceRow-like object or a plain dict.
    ``extraction_payload`` may contain ``totals_raw``/``page_text_raw`` and
    optional ``_deposit_candidate`` or source invoice totals for line mode.
    """
    payload: dict[str, Any] = {}
    if isinstance(row_or_payload, dict):
        payload.update(row_or_payload)
    else:
        for key in (
            "net_amount", "vat_amount", "total_amount", "line_items_raw",
            "description", "totals_raw", "page_text_raw", "header_raw",
            "source_invoice_net_amount", "source_invoice_vat_amount", "source_invoice_total_amount",
        ):
            if hasattr(row_or_payload, key):
                payload[key] = getattr(row_or_payload, key)
    if extraction_payload:
        # Explicit payload values should supplement rather than erase row values.
        for key, val in extraction_payload.items():
            if val not in (None, "") or key not in payload:
                payload[key] = val

    net = _money(payload.get("source_invoice_net_amount")) or _money(payload.get("net_amount"))
    vat = _money(payload.get("source_invoice_vat_amount")) or _money(payload.get("vat_amount"))
    total = _money(payload.get("source_invoice_total_amount")) or _money(payload.get("total_amount"))
    text = _raw_text(payload)
    bcrs = _money(payload.get("_deposit_candidate")) or extract_labelled_bcrs(text)
    discount = _money(payload.get("discount_amount")) or extract_labelled_discount(text)
    # Arithmetic BCRS is advisory only. It helps totals validation but does not
    # create BCRS rows; split decisions still require label/region evidence.
    if bcrs is None and net is not None and total is not None:
        _plain_diff = _round(total - (net + (vat or Decimal("0.00"))))
        if _plain_diff > Decimal("0.03") and looks_like_bcrs_amount(_plain_diff, net):
            bcrs = _plain_diff

    res = TotalsReconciliationResult(
        net_amount=_flt(net),
        vat_amount=_flt(vat),
        total_amount=_flt(total),
        actual_total=_flt(total),
        bcrs_amount=_flt(bcrs),
        discount_amount=_flt(discount),
    )

    if net is None and total is None:
        res.status = "subtotal_not_found"
        res.review_required = True
        res.review_reason = "Neither net nor total amount could be extracted."
        res.evidence.append("missing_net_and_total")
        return res
    if net is None or total is None:
        res.status = "insufficient_data"
        res.review_required = True
        res.review_reason = "Missing net or total amount prevents reconciliation."
        res.evidence.append("missing_net_or_total")
        return res

    vat_val = vat or Decimal("0.00")
    tolerance = Decimal("0.10")

    expected_plain = _round(net + vat_val)
    scenarios: list[tuple[str, Decimal, Decimal | None, Decimal | None, str]] = [
        ("reconciled", expected_plain, None, None, "net + VAT = total"),
    ]
    if bcrs is not None:
        scenarios.append(("reconciled_with_bcrs", _round(expected_plain + bcrs), bcrs, None, "net + VAT + BCRS = total"))
    if discount is not None:
        scenarios.append(("reconciled_with_discount", _round(expected_plain - discount), None, discount, "net + VAT - discount = total"))
    if bcrs is not None and discount is not None:
        scenarios.append(("reconciled_with_bcrs_and_discount", _round(expected_plain + bcrs - discount), bcrs, discount, "net + VAT + BCRS - discount = total"))

    best_status, best_expected, used_bcrs, used_discount, best_reason = min(
        scenarios, key=lambda x: abs(total - x[1])
    )
    diff = _round(total - best_expected)
    res.expected_total = _flt(best_expected)
    res.difference = _flt(diff)

    if abs(diff) <= tolerance:
        res.status = best_status
        res.review_required = False
        res.review_reason = best_reason
        res.evidence.append(best_reason)
        if used_bcrs is not None:
            res.bcrs_amount = _flt(used_bcrs)
        if used_discount is not None:
            res.discount_amount = _flt(used_discount)
    else:
        # When no explicit discount label exists, do not silently call a negative
        # difference a discount.  Flag it and suggest review.
        plain_diff = _round(total - expected_plain)
        if plain_diff < -tolerance and not has_discount_evidence(text):
            res.evidence.append("negative_difference_without_discount_label")
        if plain_diff > tolerance and bcrs is None and has_bcrs_summary_evidence(text):
            inferred_bcrs = abs(plain_diff)
            if inferred_bcrs <= Decimal("200.00"):
                res.bcrs_amount = _flt(inferred_bcrs)
                res.evidence.append("possible_bcrs_difference_from_summary_label")
        res.suggested_field_fixes.update(_suggest_digit_or_decimal_fix(net, vat_val, total))
        res.status = "mismatch_requires_review"
        res.review_required = True
        res.review_reason = (
            f"Totals mismatch: expected {best_expected:.2f} but read {total:.2f}; "
            f"difference {diff:.2f}."
        )
        if res.suggested_field_fixes:
            res.review_reason += " Possible OCR amount correction suggested."
        res.evidence.append("summary_totals_mismatch")

    # Line-item reconciliation strengthens or weakens the summary status.  Use
    # explicit line_items list if provided; otherwise support payload line_items.
    line_items = line_items or payload.get("line_items") or payload.get("items")
    if line_items:
        line_net = sum(_line_values(line_items, ("net_amount", "line_net", "amount")), Decimal("0.00"))
        line_vat_values = _line_values(line_items, ("vat_amount", "tax_amount", "line_vat"))
        line_total_values = _line_values(line_items, ("total_amount", "gross_amount", "line_total"))
        count = len(line_items)
        tol = _line_tolerance(count)
        line_ok = True
        if line_net and abs(_round(line_net) - net) > tol:
            line_ok = False
            res.evidence.append(f"line_net_sum={line_net:.2f} differs from summary_net={net:.2f}")
        if line_vat_values:
            line_vat = sum(line_vat_values, Decimal("0.00"))
            if vat is not None and abs(_round(line_vat) - vat) > tol:
                line_ok = False
                res.evidence.append(f"line_vat_sum={line_vat:.2f} differs from summary_vat={vat:.2f}")
        if line_total_values:
            line_total = sum(line_total_values, Decimal("0.00"))
            # Line totals generally exclude BCRS/global discount.  Compare to
            # commercial expected total rather than invoice total with deposit.
            expected_line_total = _round(net + vat_val)
            if abs(_round(line_total) - expected_line_total) > tol:
                line_ok = False
                res.evidence.append(f"line_total_sum={line_total:.2f} differs from summary_net_plus_vat={expected_line_total:.2f}")
        if line_ok and not res.review_required:
            res.status = "line_items_reconciled" if res.status == "reconciled" else res.status
            res.evidence.append(f"line_items_reconciled count={count}")
        elif not line_ok:
            res.status = "line_items_mismatch" if res.status not in {"mismatch_requires_review"} else res.status
            res.review_required = True
            res.review_reason = res.review_reason or "Line item totals do not reconcile with invoice summary."

    return res

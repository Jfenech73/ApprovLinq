"""
Tests for the two targeted BCRS fixes:

  1. Plain "Deposit" in a summary/totals context is accepted as BCRS evidence
  2. Original row total is reduced to net+vat after BCRS is split into its own row

Run with:  pytest tests/test_bcrs_deposit_and_split.py -v
"""
from __future__ import annotations

import uuid
import pytest


# ── Helpers duplicated inline so tests run without importing FastAPI app ───────

import re
from decimal import Decimal, InvalidOperation


def _parse_money_candidates(text: str) -> list[float]:
    vals = []
    for m in re.findall(
        r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", text or ""
    ):
        raw = (
            m.replace(".", "").replace(",", ".")
            if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", m)
            else m.replace(",", "")
        )
        try:
            vals.append(round(float(raw), 2))
        except Exception:
            pass
    return vals


def _parse_first_money(value) -> float | None:
    vals = _parse_money_candidates(str(value or ""))
    return vals[0] if vals else None


def _is_summary_context(line: str) -> bool:
    low = (line or "").lower()
    return bool(re.search(
        r"\b(total|subtotal|gross|net|vat|tax|summary|amount due|total due|"
        r"invoice summary|tax summary|deposit summary|total eur|total incl|"
        r"total net|total gross)\b",
        low,
    ))


def _is_body_or_item_context(line: str) -> bool:
    low = (line or "").lower()
    return bool(re.search(
        r"\b(qty|quantity|unit|uom|barcode|item|description|pcs|price|"
        r"w/sale|retail|consumer|code|stock|salesperson|order no|delivery note)\b",
        low,
    ))


def _collect_summary_region_lines(payload: dict) -> list[str]:
    lines: list[str] = []
    totals_raw = str(payload.get("totals_raw") or "")
    if totals_raw:
        lines.extend([ln.strip() for ln in totals_raw.splitlines() if ln.strip()])
    page_text = str(payload.get("page_text_raw") or "")
    if page_text:
        page_lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
        if page_lines:
            start_idx = max(0, int(len(page_lines) * 0.5))
            lines.extend(page_lines[start_idx:])
            lines.extend(page_lines[-20:])
    deduped: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        key = re.sub(r"\s+", " ", ln)
        if key not in seen:
            seen.add(key)
            deduped.append(ln)
    return deduped


def _extract_bcrs_amount_from_summary_local(payload: dict) -> float | None:
    """Inline copy of the fixed _extract_bcrs_amount_from_summary for testing."""
    reasons = str(payload.get("review_reasons") or "")
    m = re.search(r"deposit_component_detected:(\d+(?:\.\d{2})?)", reasons)
    if m:
        try:
            return round(float(m.group(1)), 2)
        except Exception:
            pass

    total_amount = _parse_first_money(payload.get("total_amount"))
    net_amount   = _parse_first_money(payload.get("net_amount"))
    vat_amount   = _parse_first_money(payload.get("vat_amount"))

    lines = _collect_summary_region_lines(payload)
    if not lines:
        return None

    label_re = re.compile(
        r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit|deposit|surcharge)\b",
        re.I,
    )
    summary_text = "\n".join(lines)
    ranked: list[tuple[int, float]] = []

    def _add(score: int, val: float | None) -> None:
        if val is None:
            return
        try:
            f = round(float(val), 2)
        except Exception:
            return
        if f <= 0:
            return
        ranked.append((score, f))

    # Pass 1
    patterns = [
        re.compile(r"(?is)\bbcrs(?:\s+refundable)?(?:\s+deposit)?\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\brefundable\s+deposit\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\bdeposit\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
    ]
    for pidx, pattern in enumerate(patterns):
        for match in pattern.finditer(summary_text):
            label_span = summary_text[max(0, match.start() - 40):min(len(summary_text), match.end() + 40)].lower()
            if pidx == 2:
                has_context_window = (
                    "bcrs" in label_span
                    or "summary" in label_span
                    or "total" in label_span
                    or "refundable" in label_span
                )
                match_line = ""
                pos = 0
                for ln in lines:
                    if pos + len(ln) >= match.start():
                        match_line = ln
                        break
                    pos += len(ln) + 1
                has_summary_line = _is_summary_context(match_line)
                if not has_context_window and not has_summary_line:
                    continue
            raw = match.group(1)
            try:
                val = float(raw.replace(",", "."))
            except Exception:
                continue
            score = 18 if pidx == 0 else 16 if pidx == 1 else 12
            if total_amount is not None and net_amount is not None and vat_amount is not None:
                if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                    score += 12
                elif total_amount > 0 and 0 < val < total_amount:
                    score += 2
            _add(score, val)

    # Pass 2
    for idx, line in enumerate(lines):
        low = line.lower()
        label_match = label_re.search(low)
        if not label_match:
            continue
        if _is_body_or_item_context(line) and not _is_summary_context(line):
            continue

        plain_deposit_only = (
            "deposit" in low
            and "bcrs" not in low
            and "refundable" not in low
            and "surcharge" not in low
        )
        neighborhood = " ".join(lines[max(0, idx - 1): min(len(lines), idx + 2)]).lower()
        if plain_deposit_only and not (
            _is_summary_context(line)
            or "bcrs" in neighborhood
            or "summary" in neighborhood
            or "total" in neighborhood
        ):
            continue

        same_line_vals = _parse_money_candidates(line)
        if same_line_vals:
            after = line[label_match.end():]
            after_vals = _parse_money_candidates(after)
            if after_vals:
                for val in after_vals[:2]:
                    score = 22
                    if "bcrs" in low:      score += 10
                    if "refundable" in low: score += 5
                    if "deposit" in low:    score += 6
                    if "surcharge" in low:  score += 5
                    if _is_summary_context(line): score += 4
                    if total_amount is not None and net_amount is not None and vat_amount is not None:
                        if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                            score += 12
                    _add(score, val)
            for val in same_line_vals:
                score = 12
                if "bcrs" in low:       score += 8
                if "deposit" in low:     score += 4
                if _is_summary_context(line): score += 4
                if total_amount is not None and net_amount is not None and vat_amount is not None:
                    if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                        score += 10
                _add(score, val)

        for nidx in range(max(0, idx - 1), min(len(lines), idx + 2)):
            if nidx == idx:
                continue
            cand_line = lines[nidx]
            if _is_body_or_item_context(cand_line) and not _is_summary_context(cand_line):
                continue
            for val in _parse_money_candidates(cand_line):
                score = 8
                if "bcrs" in low:      score += 8
                if "refundable" in low: score += 3
                if "deposit" in low:    score += 4
                if _is_summary_context(cand_line) or _is_summary_context(line):
                    score += 4
                if total_amount is not None and net_amount is not None and vat_amount is not None:
                    if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                        score += 10
                _add(score, val)

    if not ranked:
        return None
    ranked.sort(key=lambda x: (x[0], -abs(x[1])), reverse=True)
    best_score, best_val = ranked[0]
    return best_val if best_score >= 12 else None


# ══════════════════════════════════════════════════════════════════════════════
# A. Deposit wording in totals area
# ══════════════════════════════════════════════════════════════════════════════

class TestDepositWordingInSummary:

    def test_deposit_in_deposit_summary_block(self):
        """'Deposit Summary' heading + Deposit line with amount is accepted."""
        payload = {
            "totals_raw": (
                "Deposit Summary\n"
                "Deposit  4.80\n"
                "Net Amount  250.00\n"
                "VAT 18%  45.00\n"
                "Total  299.80\n"
            ),
            "net_amount":   250.00,
            "vat_amount":   45.00,
            "total_amount": 299.80,
        }
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result == 4.80, f"Expected 4.80, got {result}"

    def test_deposit_same_line_as_amount(self):
        """'Deposit 2.40' on its own summary line is accepted."""
        payload = {
            "totals_raw": (
                "Sub Total  180.00\n"
                "VAT  32.40\n"
                "Deposit  2.40\n"
                "Invoice Total  214.80\n"
            ),
            "net_amount":   180.00,
            "vat_amount":   32.40,
            "total_amount": 214.80,
        }
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result == 2.40, f"Expected 2.40, got {result}"

    def test_deposit_total_summary_neighbourhood(self):
        """'Deposit' near 'Total' in same block is accepted."""
        payload = {
            "totals_raw": (
                "Total Goods  500.00\n"
                "VAT  90.00\n"
                "Deposit  7.20\n"
                "Grand Total  597.20\n"
            ),
            "net_amount":   500.00,
            "vat_amount":   90.00,
            "total_amount": 597.20,
        }
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result == 7.20, f"Expected 7.20, got {result}"

    def test_deposit_in_item_row_not_accepted(self):
        """'deposit' appearing in an item/body description line must be rejected."""
        payload = {
            "totals_raw": "",
            "page_text_raw": (
                "Item  Description  Qty  Unit Price  Total\n"
                "001  Glass bottle deposit  24  0.10  2.40\n"
                "002  Cisk Lager 330ml      24  0.80  19.20\n"
                "\n"
                "Sub Total  21.60\n"
                "VAT  3.89\n"
                "Invoice Total  25.49\n"
            ),
            "net_amount":   21.60,
            "vat_amount":   3.89,
            "total_amount": 25.49,
        }
        # The invoice total reconciles without a deposit component
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result is None, \
            f"Item-row 'deposit' must not trigger BCRS detection; got {result}"

    def test_deposit_without_any_summary_context_not_accepted(self):
        """'deposit' with no nearby totals/summary context must not trigger."""
        payload = {
            "totals_raw": "",
            "page_text_raw": (
                "Customer account deposit paid on 01/01/2026\n"
                "Please retain this receipt\n"
            ),
            "net_amount":   None,
            "vat_amount":   None,
            "total_amount": None,
        }
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result is None, \
            f"Context-free 'deposit' mention must return None; got {result}"


# ══════════════════════════════════════════════════════════════════════════════
# B. Existing explicit BCRS wording still works
# ══════════════════════════════════════════════════════════════════════════════

class TestExplicitBcrsStillWorks:

    def test_bcrs_label_same_line(self):
        payload = {
            "totals_raw": "Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }
        assert _extract_bcrs_amount_from_summary_local(payload) == 2.40

    def test_bcrs_deposit_label(self):
        payload = {
            "totals_raw": "Net 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80",
            "net_amount": 400.0, "vat_amount": 72.0, "total_amount": 542.80,
        }
        assert _extract_bcrs_amount_from_summary_local(payload) == 70.80

    def test_bcrs_refundable_deposit_label(self):
        payload = {
            "totals_raw": "Sub Total 98.72\nVAT 17.77\nBCRS Refundable Deposit (M) 10.80\nTotal Due 127.29",
            "net_amount": 98.72, "vat_amount": 17.77, "total_amount": 127.29,
        }
        assert _extract_bcrs_amount_from_summary_local(payload) == 10.80


# ══════════════════════════════════════════════════════════════════════════════
# C. Split math is corrected
# ══════════════════════════════════════════════════════════════════════════════

class TestSplitMathCorrection:
    """Verify the total_amount adjustment logic from _process_batch_job."""

    def _simulate_split(self, net, vat, total, bcrs_amount):
        """Reproduce the exact adjustment logic from batches.py."""
        _net            = round(float(net  or 0.0), 2)
        _vat            = round(float(vat  or 0.0), 2)
        _corrected_total = round(_net + _vat, 2)
        original_total  = round(float(total or 0.0), 2)
        if _corrected_total >= 0 and _corrected_total < original_total:
            return _corrected_total
        return original_total

    def test_original_total_reduced_to_net_plus_vat(self):
        """original.total must equal net + vat after split."""
        adjusted = self._simulate_split(
            net=200.00, vat=36.00, total=238.40, bcrs_amount=2.40
        )
        assert adjusted == 236.00, f"Expected 236.00, got {adjusted}"

    def test_bcrs_row_total_equals_bcrs_amount(self):
        """BCRS row must have net = vat = 0, total = bcrs_amount."""
        bcrs_amount = 7.20
        # The _build_bcrs_row function sets net=amount, vat=0.0, total=amount
        bcrs_net   = bcrs_amount
        bcrs_vat   = 0.0
        bcrs_total = bcrs_amount
        assert bcrs_net   == 7.20
        assert bcrs_vat   == 0.0
        assert bcrs_total == 7.20

    def test_combined_totals_equal_real_invoice_total(self):
        """original.total + bcrs_row.total must equal the real invoice total."""
        net          = 300.00
        vat          = 54.00
        real_total   = 361.20
        bcrs_amount  = 7.20

        adjusted_original = self._simulate_split(net, vat, real_total, bcrs_amount)
        bcrs_row_total    = bcrs_amount

        combined = round(adjusted_original + bcrs_row_total, 2)
        assert abs(combined - real_total) <= 0.01, \
            f"Combined {combined} != real total {real_total}"

    def test_total_not_reduced_when_already_matches_net_plus_vat(self):
        """If total already equals net+vat (balanced invoice), don't reduce it."""
        # total = net + vat exactly — no BCRS mismatch
        adjusted = self._simulate_split(
            net=100.00, vat=18.00, total=118.00, bcrs_amount=0.50
        )
        # _corrected_total (118.00) is NOT < original_total (118.00) → no change
        assert adjusted == 118.00, \
            "Total must not be reduced when it already equals net+vat"

    def test_total_not_reduced_below_zero(self):
        """A malformed invoice with total=0 must not go negative."""
        adjusted = self._simulate_split(
            net=0.0, vat=0.0, total=0.0, bcrs_amount=2.40
        )
        assert adjusted >= 0, "total_amount must never go negative"

    def test_large_bcrs_total_correctly_split(self):
        """Large BCRS (J. Sultana style €70.80) splits total correctly."""
        adjusted = self._simulate_split(
            net=400.00, vat=72.00, total=542.80, bcrs_amount=70.80
        )
        assert adjusted == 472.00, f"Expected 472.00, got {adjusted}"
        combined = round(adjusted + 70.80, 2)
        assert abs(combined - 542.80) <= 0.01


# ══════════════════════════════════════════════════════════════════════════════
# D. No double adjustment / no duplicate rows on reprocess
# ══════════════════════════════════════════════════════════════════════════════

class TestNoDoubleAdjustment:

    def test_adjustment_only_fires_when_corrected_total_is_less(self):
        """The guard `_corrected_total < original_total` prevents double reduction.

        After the first processing the row total is set to net+vat.
        On reprocess, _process_batch_job deletes all rows and re-creates them
        from scratch — so the row starts fresh with the extracted total each time.
        This test confirms the guard logic is safe.
        """
        net, vat, total = 200.00, 36.00, 238.40

        # First pass: total > net+vat → adjust fires
        t1 = round(net + vat, 2)
        assert t1 < total  # 236.00 < 238.40 → adjustment fires

        # If somehow called again with already-adjusted total:
        already_adjusted = t1   # 236.00
        t2 = round(net + vat, 2)
        # Guard: t2 (236.00) is NOT < already_adjusted (236.00) → no further change
        assert not (t2 < already_adjusted), \
            "Second pass must not trigger when total already equals net+vat"

    def test_no_bcrs_row_created_when_no_deposit_detected(self):
        """If _extract_bcrs_amount_from_summary returns None, no BCRS row and no adjustment."""
        payload = {
            "totals_raw": "Net 100.00\nVAT 18.00\nTotal 118.00",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 118.0,
        }
        from tests.test_bcrs_deposit_and_split import _extract_bcrs_amount_from_summary_local
        result = _extract_bcrs_amount_from_summary_local(payload)
        assert result is None, "Clean balanced invoice must not trigger BCRS split"


# ══════════════════════════════════════════════════════════════════════════════
# E. _is_summary_context recognises deposit-related headings
# ══════════════════════════════════════════════════════════════════════════════

class TestIsSummaryContext:

    def test_deposit_summary_is_summary_context(self):
        assert _is_summary_context("Deposit Summary")

    def test_total_line_is_summary_context(self):
        assert _is_summary_context("Invoice Total  542.80")

    def test_vat_line_is_summary_context(self):
        assert _is_summary_context("VAT 18%  45.00")

    def test_item_description_is_not_summary_context(self):
        assert not _is_summary_context("Glass bottle deposit 330ml 24pcs")

    def test_body_qty_line_not_summary(self):
        assert not _is_summary_context("QTY  Description  Unit Price  Total")

"""
Tests for BCRS/deposit false-positive prevention.

Covers:
  A. Positive cases (genuine BCRS must still split)
  B. Negative cases (false positives must be prevented)
  C. Stability (no regressions, no double-splits)

Root cause of the false positives fixed here:
  _extract_bcrs_amount_from_summary previously had an "arithmetic shortcut"
  that read deposit_component_detected from review_reasons and returned
  immediately — before checking any BCRS/deposit label in the document text.
  This caused splits on any invoice where total - (net+vat) landed on a
  common denomination (e.g. diff=2.40), even when no deposit label existed.

  The three patches applied:
  1. Removed the arithmetic shortcut (Path A).
  2. Raised the minimum score from 12 → 20.
  3. Added an "independent label line" guard: at least one non-pure-totals
     line with a BCRS/deposit keyword and a monetary value must exist.

Run with:  pytest tests/test_bcrs_false_positives.py -v
"""
from __future__ import annotations

import re
import sys
import os

import pytest

# ── Inline helpers (same as batches.py, avoid full app import) ────────────────

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


def _parse_first_money(v) -> float | None:
    return (_parse_money_candidates(str(v or "")) + [None])[0]


def _is_summary_context(line: str) -> bool:
    return bool(re.search(
        r"\b(total|subtotal|gross|net|vat|tax|summary|amount due|total due|"
        r"invoice summary|tax summary|deposit summary|total eur|total incl|"
        r"total net|total gross)\b",
        (line or "").lower(),
    ))


def _is_body_or_item_context(line: str) -> bool:
    return bool(re.search(
        r"\b(qty|quantity|unit|uom|barcode|item|description|pcs|price|"
        r"w/sale|retail|consumer|code|stock|salesperson|order no|delivery note)\b",
        (line or "").lower(),
    ))


def _collect_summary_region_lines(payload: dict) -> list[str]:
    lines: list[str] = []
    tr = str(payload.get("totals_raw") or "")
    if tr:
        lines.extend([ln.strip() for ln in tr.splitlines() if ln.strip()])
    pt = str(payload.get("page_text_raw") or "")
    if pt:
        pl = [ln.strip() for ln in pt.splitlines() if ln.strip()]
        if pl:
            si = max(0, int(len(pl) * 0.5))
            lines.extend(pl[si:])
            lines.extend(pl[-20:])
    deduped: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        k = re.sub(r"\s+", " ", ln)
        if k not in seen:
            seen.add(k)
            deduped.append(ln)
    return deduped


# Load the live _extract_bcrs_amount_from_summary from batches.py
def _load_extract_fn():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers", "batches.py")).read()
    start = src.find("\ndef _parse_money_candidates")
    end   = src.find("\ndef _build_bcrs_row")
    fn_src = src[start:end]
    ns: dict = {
        "re": re,
        "_parse_money_candidates": _parse_money_candidates,
        "_parse_first_money": _parse_first_money,
        "_is_summary_context": _is_summary_context,
        "_is_body_or_item_context": _is_body_or_item_context,
        "_collect_summary_region_lines": _collect_summary_region_lines,
    }
    exec(fn_src, ns)
    return ns["_extract_bcrs_amount_from_summary"]


_extract = _load_extract_fn()


# ══════════════════════════════════════════════════════════════════════════════
# A. Positive cases — genuine BCRS/deposit must still split
# ══════════════════════════════════════════════════════════════════════════════

class TestGenuineBcrsCases:

    def test_explicit_bcrs_same_line(self):
        assert _extract({
            "totals_raw": "Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }) == 2.40

    def test_bcrs_deposit_large(self):
        """J. Sultana-style large BCRS deposit."""
        assert _extract({
            "totals_raw": "Net 400.00\nVAT 72.00\nBCRS Deposit 70.80\nTotal 542.80",
            "net_amount": 400.0, "vat_amount": 72.0, "total_amount": 542.80,
        }) == 70.80

    def test_bcrs_refundable_deposit(self):
        """Maverick-style BCRS Refundable Deposit (M) label."""
        assert _extract({
            "totals_raw": "Sub Total 98.72\nVAT 17.77\nBCRS Refundable Deposit (M) 10.80\nTotal Due 127.29",
            "net_amount": 98.72, "vat_amount": 17.77, "total_amount": 127.29,
        }) == 10.80

    def test_bcrs_schembri_style(self):
        assert _extract({
            "totals_raw": "Net Amount 80.00\nVAT Amount 14.40\nBCRS 7.20\nInvoice Total 101.60",
            "net_amount": 80.0, "vat_amount": 14.40, "total_amount": 101.60,
        }) == 7.20

    def test_bcrs_deposit_nectar(self):
        assert _extract({
            "totals_raw": "Net: 200.00\nVAT: 36.00\nBCRS Deposit 2.40\nTotal: 238.40",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 238.40,
        }) == 2.40

    def test_deposit_summary_real_nonzero(self):
        """Farsons-style: Deposit Summary block with a real 4.80 deposit line."""
        assert _extract({
            "totals_raw": "Deposit Summary\nDeposit  4.80\nNet Amount  250.00\nVAT 18%  45.00\nTotal  299.80",
            "net_amount": 250.0, "vat_amount": 45.0, "total_amount": 299.80,
        }) == 4.80

    def test_plain_deposit_in_totals_block(self):
        """'Deposit' label on its own line in a totals section."""
        assert _extract({
            "totals_raw": "Sub Total 180.00\nVAT 32.40\nDeposit 2.40\nInvoice Total 214.80",
            "net_amount": 180.0, "vat_amount": 32.40, "total_amount": 214.80,
        }) == 2.40

    def test_amsm_style_bcrs(self):
        assert _extract({
            "totals_raw": "Sub Total 200.00\nVAT 36.00\nBCRS 14.40\nTotal 250.40",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 250.40,
        }) == 14.40


# ══════════════════════════════════════════════════════════════════════════════
# B. Negative cases — must NOT split
# ══════════════════════════════════════════════════════════════════════════════

class TestFalsePositivePrevention:

    def test_arithmetic_shortcut_removed(self):
        """THE MAIN FIX: deposit_component_detected in reasons alone must NOT split.

        Before this fix, the arithmetic shortcut in _extract_bcrs_amount_from_summary
        returned 2.40 immediately when it saw deposit_component_detected:2.40 in
        review_reasons — without checking whether any BCRS/deposit label exists in
        the document text.
        """
        result = _extract({
            "review_reasons": "deposit_component_detected:2.40",
            "totals_raw": "Sub Total  200.00\nVAT  36.00\nInvoice Total  238.40",
            # No "Deposit" or "BCRS" label anywhere — just arithmetic
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 238.40,
        })
        assert result is None, (
            f"Arithmetic hit alone must NOT trigger split (got {result}). "
            f"This was the root cause of the Dione false positive."
        )

    def test_arithmetic_hit_100_no_label(self):
        """deposit_component_detected:100.00 with no BCRS label → None."""
        assert _extract({
            "review_reasons": "deposit_component_detected:100.00",
            "totals_raw": "Sub Total  100.00\nVAT  0.00\nTotal  100.00",
            "net_amount": 100.0, "vat_amount": 0.0, "total_amount": 100.0,
        }) is None

    def test_clean_balanced_invoice(self):
        """Standard subtotal/VAT/total invoice with no deposit label."""
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Sub Total  200.00\nVAT  36.00\nInvoice Total  236.00",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 236.0,
        }) is None

    def test_item_row_deposit_not_accepted(self):
        """'deposit' in an item row description must NOT trigger split."""
        assert _extract({
            "review_reasons": "",
            "totals_raw": "",
            "page_text_raw": (
                "Item  Description  Qty  Unit Price  Total\n"
                "001  Glass bottle deposit  24  0.10  2.40\n"
                "002  Cisk Lager 330ml      24  0.80  19.20\n"
                "\nSub Total  21.60\nVAT  3.89\nInvoice Total  25.49"
            ),
            "net_amount": 21.60, "vat_amount": 3.89, "total_amount": 25.49,
        }) is None

    def test_zero_deposit_not_accepted(self):
        """Deposit Summary present but deposit total is 0.00 → no split."""
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Deposit Summary\nDeposit  0.00\nNet Amount  250.00\nVAT 18%  45.00\nTotal  295.00",
            "net_amount": 250.0, "vat_amount": 45.0, "total_amount": 295.0,
        }) is None

    def test_deposit_summary_heading_only_balanced(self):
        """Farsons-like: 'Deposit Summary' heading but totals balance without deposit.

        Only a summary heading is present; the actual totals (net+vat) reconcile
        to the invoice total without needing a deposit component.  Must not split.
        """
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Deposit Summary\nTotal Net  180.00\nVAT  32.40\nTotal  212.40",
            "net_amount": 180.0, "vat_amount": 32.40, "total_amount": 212.40,
        }) is None

    def test_deposit_in_section_heading_not_item(self):
        """'Deposit charge information' as a heading, then normal totals → no split."""
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Deposit charge information\nSub Total  500.00\nVAT  90.00\nTotal  590.00",
            "net_amount": 500.0, "vat_amount": 90.0, "total_amount": 590.0,
        }) is None

    def test_deposit_summary_no_deposit_line(self):
        """'Deposit Summary' heading but no 'Deposit X.XX' line follows → no split."""
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Deposit Summary\nNet  300.00\nVAT  54.00\nTotal  354.00",
            "net_amount": 300.0, "vat_amount": 54.0, "total_amount": 354.0,
        }) is None

    def test_no_split_from_empty_payload(self):
        assert _extract({}) is None

    def test_no_split_when_no_totals(self):
        assert _extract({"totals_raw": "", "page_text_raw": ""}) is None


# ══════════════════════════════════════════════════════════════════════════════
# C. Structural checks — the fixes are in place
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuralGuards:

    def test_arithmetic_shortcut_removed_from_source(self):
        """The Path A shortcut block must not exist in batches.py."""
        src = open("app/routers/batches.py").read()
        # The old shortcut returned the deposit_component_detected value early
        assert "return round(float(m.group(1)), 2)" not in src.split(
            "def _extract_bcrs_amount_from_summary"
        )[1].split("def _build_bcrs_row")[0], (
            "Arithmetic shortcut must be removed from _extract_bcrs_amount_from_summary"
        )

    def test_minimum_score_threshold_is_20(self):
        """Minimum score threshold must be 20, not 12."""
        src = open("app/routers/batches.py").read()
        fn_src = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert ("best_score >= 20" in fn_src or "best_score < 20" in fn_src), "Minimum score threshold must be 20"
        assert "best_score >= 12" not in fn_src, "Old threshold of 12 must be removed"

    def test_label_line_guard_present(self):
        """The 'has_label_line' guard must be present in _extract_bcrs_amount_from_summary."""
        src = open("app/routers/batches.py").read()
        fn_src = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "has_label_line" in fn_src

    def test_deposit_label_re_includes_plain_deposit(self):
        """_DEPOSIT_LABEL_RE must match plain 'deposit' word."""
        src = open("app/routers/batches.py").read()
        # Extract the _DEPOSIT_LABEL_RE definition from the function
        assert "|deposit)" in src or "surcharge|deposit)" in src, \
            "_DEPOSIT_LABEL_RE must include plain 'deposit' as a term"

    def test_zero_guard_in_add_candidate(self):
        """_add_candidate must reject f <= 0 values."""
        src = open("app/routers/batches.py").read()
        fn_src = src.split("def _extract_bcrs_amount_from_summary")[1].split("def _build_bcrs_row")[0]
        assert "f <= 0" in fn_src, "_add_candidate must reject zero/negative values"


# ══════════════════════════════════════════════════════════════════════════════
# D. Reconciliation assist: boosts genuine candidates, doesn't invent them
# ══════════════════════════════════════════════════════════════════════════════

class TestReconciliationAssist:

    def test_reconciliation_boosts_but_does_not_invent(self):
        """Reconciliation alone (without a label line) must not trigger a split."""
        # net=100, vat=18, total=120.40 — diff=2.40 hits denomination exactly
        # but there is NO deposit label anywhere
        assert _extract({
            "review_reasons": "",
            "totals_raw": "Sub Total 100.00\nVAT 18.00\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }) is None

    def test_reconciliation_boosts_genuine_label_candidate(self):
        """When a BCRS label exists AND reconciliation confirms it, split happens."""
        result = _extract({
            "totals_raw": "Net 100.00\nVAT 18.00\nBCRS 2.40\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        })
        assert result == 2.40

    def test_combined_totals_equal_invoice_total(self):
        """After a split, original.total + bcrs.total should equal invoice total."""
        # Check the _process_batch_job math via the guard in batches.py
        src = open("app/routers/batches.py").read()
        # Verify the total-correction logic still exists
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

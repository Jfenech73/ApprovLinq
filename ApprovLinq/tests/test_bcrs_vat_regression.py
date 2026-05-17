"""
Regression tests for BCRS/deposit split detection.

Root causes fixed:
  1. _parse_first_money: str(61.80) == '61.8' loses the trailing zero, causing
     the two-decimal-place regex to return None for net_amount.  Fixed by
     handling numeric inputs directly without re-parsing through the regex.

  2. "Total incl VAT & BCRS 80.12" lines were scored as BCRS candidates because
     they contain the word "BCRS".  These lines carry the invoice total, not the
     BCRS amount.  Fixed with _is_total_incl_line() hard rejection in Pass 1+2.

  3. VAT/tax rows were reachable as neighbour candidates of BCRS label lines.
     Fixed with _is_vat_line() hard rejection in Pass 2 neighbour scan.

Run: pytest tests/test_bcrs_vat_regression.py -v
"""
from __future__ import annotations
import re, os


# ── Load live _extract_bcrs_amount_from_summary ────────────────────────────────

def _load():
    def _pm(t):
        vals = []
        for m in re.findall(
            r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", t or ""
        ):
            raw = (m.replace(".", "").replace(",", ".")
                   if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", m)
                   else m.replace(",", ""))
            try:
                vals.append(round(float(raw), 2))
            except Exception:
                pass
        return vals

    def _fm(v):
        return (_pm(str(v or "")) + [None])[0]

    def _isc(l):
        return bool(re.search(
            r"\b(total|subtotal|gross|net|vat|tax|summary|amount due|total due|"
            r"invoice summary|tax summary|deposit summary|total eur|total incl|"
            r"total net|total gross)\b",
            (l or "").lower(),
        ))

    def _ibi(l):
        return bool(re.search(
            r"\b(qty|quantity|unit|uom|barcode|item|description|pcs|price|"
            r"w/sale|retail|consumer|code|stock|salesperson|order no|delivery note)\b",
            (l or "").lower(),
        ))

    def _csr(payload):
        lines = []
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
        deduped, seen = [], set()
        for ln in lines:
            k = re.sub(r"\s+", " ", ln)
            if k not in seen:
                seen.add(k)
                deduped.append(ln)
        return deduped

    src = open(os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "batches.py"
    )).read()
    start = src.find("\ndef _parse_money_candidates")
    end   = src.find("\ndef _build_bcrs_row")
    ns = {
        "re": re,
        "_parse_money_candidates": _pm,
        "_parse_first_money": _fm,
        "_is_summary_context": _isc,
        "_is_body_or_item_context": _ibi,
        "_collect_summary_region_lines": _csr,
    }
    exec(src[start:end], ns)
    return ns["_extract_bcrs_amount_from_summary"]


_extract = _load()


# ═══════════════════════════════════════════════════════════════════════════════
# Spec examples — all five verified invoices must match exactly
# ═══════════════════════════════════════════════════════════════════════════════

class TestSpecExamples:
    def test_amsm(self):
        """AMSM: Net 132.00, VAT 23.76, BCRS 14.40, Total 170.16"""
        assert _extract({
            "totals_raw": "Net 132.00\nVAT 23.76\nBCRS 14.40\nTotal 170.16",
            "net_amount": 132.0, "vat_amount": 23.76, "total_amount": 170.16,
        }) == 14.40

    def test_schembri(self):
        """Schembri: Total excl VAT 61.80, VAT 11.12, BCRS 7.20,
        Total incl VAT & BCRS 80.12 — must return 7.20, NOT 80.12."""
        result = _extract({
            "totals_raw": (
                "Total excl VAT 61.80\nVAT 11.12\nBCRS 7.20\n"
                "Total incl VAT & BCRS 80.12"
            ),
            "net_amount": 61.80, "vat_amount": 11.12, "total_amount": 80.12,
        })
        assert result == 7.20, (
            f"Expected 7.20, got {result}. "
            "'Total incl VAT & BCRS 80.12' must not be chosen as the BCRS amount."
        )

    def test_maverick(self):
        """Maverick: Total Net 77.52, VAT 10.40, BCRS Refundable Deposit 10.80,
        Invoice Total 98.72 — must return 10.80, NOT 10.40 (VAT)."""
        result = _extract({
            "totals_raw": (
                "Total Net 77.52\nVAT 10.40\nBCRS Refundable Deposit 10.80\n"
                "Invoice Total 98.72"
            ),
            "net_amount": 77.52, "vat_amount": 10.40, "total_amount": 98.72,
        })
        assert result == 10.80, (
            f"Expected 10.80, got {result}. VAT (10.40) must never be selected as BCRS."
        )

    def test_j_sultana(self):
        """J. Sultana: Subtotal 526.22, VAT 94.72, BCRS Deposit 70.80, Total 691.74"""
        assert _extract({
            "totals_raw": "Subtotal 526.22\nVAT 94.72\nBCRS Deposit 70.80\nTotal 691.74",
            "net_amount": 526.22, "vat_amount": 94.72, "total_amount": 691.74,
        }) == 70.80

    def test_nectar(self):
        """Nectar: Net 76.26, VAT 13.73, BCRS Deposit 2.40, Total Due 92.39"""
        assert _extract({
            "totals_raw": "Net 76.26\nVAT 13.73\nBCRS Deposit 2.40\nTotal Due 92.39",
            "net_amount": 76.26, "vat_amount": 13.73, "total_amount": 92.39,
        }) == 2.40


# ═══════════════════════════════════════════════════════════════════════════════
# VAT-as-BCRS regression — core of the regression fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestVatAsBcrsRejection:
    """VAT rows and total-inclusive lines must never be selected as BCRS."""

    def test_vat_row_never_selected_when_bcrs_present(self):
        """When BCRS ≈ VAT in value, must always pick the BCRS row."""
        # VAT=10.40, BCRS=10.80 — very close values, must pick BCRS
        result = _extract({
            "totals_raw": (
                "Net 77.52\nVAT 10.40\nBCRS Refundable Deposit 10.80\n"
                "Invoice Total 98.72"
            ),
            "net_amount": 77.52, "vat_amount": 10.40, "total_amount": 98.72,
        })
        assert result == 10.80
        assert result != 10.40, "VAT (10.40) was incorrectly selected as BCRS"

    def test_total_incl_bcrs_line_rejected(self):
        """'Total incl VAT & BCRS X.XX' is an invoice total, not a BCRS amount."""
        result = _extract({
            "totals_raw": (
                "Net 61.80\nVAT 11.12\nBCRS 7.20\n"
                "Total incl VAT & BCRS 80.12"
            ),
            "net_amount": 61.80, "vat_amount": 11.12, "total_amount": 80.12,
        })
        assert result == 7.20
        assert result != 80.12, "'Total incl VAT & BCRS 80.12' (invoice total) was incorrectly selected"

    def test_invoice_total_incl_bcrs_rejected(self):
        """'Invoice Total incl BCRS X.XX' is an invoice total, not a BCRS amount."""
        result = _extract({
            "totals_raw": (
                "Net 77.52\nVAT 10.40\nBCRS 10.80\n"
                "Invoice Total incl BCRS 98.72"
            ),
            "net_amount": 77.52, "vat_amount": 10.40, "total_amount": 98.72,
        })
        assert result == 10.80
        assert result != 98.72

    def test_float_net_amount_parses_correctly(self):
        """Root-cause fix: str(61.80)=='61.8' must not cause net_amount to be None.

        When net_amount is None the reconciliation check is skipped, potentially
        allowing the wrong candidate to win.  This tests the _parse_first_money fix.
        """
        src = open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "batches.py"
        )).read()
        # Extract _parse_first_money from the live code and test it directly
        start = src.find("\ndef _parse_money_candidates")
        end   = src.find("\ndef _collect_summary_region_lines")
        ns: dict = {"re": re}
        exec(src[start:end], ns)
        _fm = ns["_parse_first_money"]

        # These must all return a non-None float
        assert _fm(61.80) == 61.80, "float 61.80 must parse correctly"
        assert _fm(61.8)  == 61.80, "float 61.8 must parse correctly"
        assert _fm(10.40) == 10.40, "float 10.40 must parse correctly"
        assert _fm(0.0)   is None,  "zero must return None"
        assert _fm(None)  is None,  "None must return None"

    def test_vat_line_helper_detects_vat_rows(self):
        src = open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "batches.py"
        )).read()
        start = src.find("\ndef _is_vat_line")
        end   = src.find("\ndef _is_total_incl_line")
        ns: dict = {"re": re}
        exec(src[start:end], ns)
        _is_vat = ns["_is_vat_line"]

        assert _is_vat("VAT 11.12")
        assert _is_vat("VAT Amount 23.76")
        assert _is_vat("V.A.T. 10.40")
        assert _is_vat("Tax Amount 94.72")
        assert not _is_vat("BCRS 7.20")
        assert not _is_vat("BCRS Deposit 2.40")
        assert not _is_vat("Total 98.72")

    def test_total_incl_line_helper_detects_total_lines(self):
        src = open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "batches.py"
        )).read()
        start = src.find("\ndef _is_total_incl_line")
        end   = src.find("\n\ndef _collect_summary_region_lines")
        ns: dict = {"re": re}
        exec(src[start:end], ns)
        _is_total = ns["_is_total_incl_line"]

        assert _is_total("Total incl VAT & BCRS 80.12")
        assert _is_total("Invoice Total 98.72")
        assert _is_total("Grand Total 170.16")
        assert _is_total("Amount Due 92.39")
        assert not _is_total("BCRS 7.20")
        assert not _is_total("BCRS Deposit 70.80")
        assert not _is_total("Net 77.52")
        assert not _is_total("VAT 10.40")


# ═══════════════════════════════════════════════════════════════════════════════
# Must-NOT-split — false positives must remain None
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoSplit:
    def test_clean_balanced_invoice(self):
        assert _extract({
            "totals_raw": "Sub Total 200.00\nVAT 36.00\nInvoice Total 236.00",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 236.0,
        }) is None

    def test_arithmetic_mismatch_no_label(self):
        assert _extract({
            "review_reasons": "deposit_component_detected:2.40",
            "totals_raw": "Sub Total 200.00\nVAT 36.00\nInvoice Total 238.40",
            "net_amount": 200.0, "vat_amount": 36.0, "total_amount": 238.40,
        }) is None

    def test_reconciliation_only_no_label(self):
        assert _extract({
            "totals_raw": "Sub Total 100.00\nVAT 18.00\nTotal 120.40",
            "net_amount": 100.0, "vat_amount": 18.0, "total_amount": 120.40,
        }) is None

    def test_zero_deposit(self):
        assert _extract({
            "totals_raw": "Deposit Summary\nDeposit 0.00\nNet 250.00\nVAT 45.00\nTotal 295.00",
            "net_amount": 250.0, "vat_amount": 45.0, "total_amount": 295.0,
        }) is None

    def test_item_row_deposit_ignored(self):
        assert _extract({
            "page_text_raw": (
                "Item  Description  Qty  Unit  Total\n"
                "001  Glass bottle deposit  24  0.10  2.40\n"
                "Sub Total 21.60\nVAT 3.89\nInvoice Total 25.49"
            ),
            "net_amount": 21.60, "vat_amount": 3.89, "total_amount": 25.49,
        }) is None

    def test_deposit_heading_balanced(self):
        assert _extract({
            "totals_raw": "Deposit Summary\nTotal Net 180.00\nVAT 32.40\nTotal 212.40",
            "net_amount": 180.0, "vat_amount": 32.40, "total_amount": 212.40,
        }) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Structural checks — helpers and wiring are present
# ═══════════════════════════════════════════════════════════════════════════════

class TestStructural:
    def _src(self):
        return open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "batches.py"
        )).read()

    def test_parse_first_money_handles_floats(self):
        src = self._src()
        fn = src[src.find("def _parse_first_money"):
                 src.find("\ndef _is_summary_context")]
        assert "isinstance(value, (int, float))" in fn, \
            "_parse_first_money must handle numeric inputs directly"

    def test_is_vat_line_defined(self):
        assert "def _is_vat_line" in self._src()

    def test_is_total_incl_line_defined(self):
        assert "def _is_total_incl_line" in self._src()

    def test_vat_line_rejection_in_pass2(self):
        src = self._src()
        fn = src[src.find("def _extract_bcrs_amount_from_summary"):
                 src.find("\ndef _build_bcrs_row")]
        assert "_is_vat_line(line)" in fn, \
            "Pass 2 must reject VAT lines as BCRS candidates"

    def test_total_incl_rejection_in_pass1_and_pass2(self):
        src = self._src()
        fn = src[src.find("def _extract_bcrs_amount_from_summary"):
                 src.find("\ndef _build_bcrs_row")]
        assert fn.count("_is_total_incl_line") >= 2, \
            "_is_total_incl_line must be checked in both Pass 1 and Pass 2"

    def test_split_math_preserved(self):
        src = self._src()
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

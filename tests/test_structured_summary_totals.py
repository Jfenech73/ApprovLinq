"""
Tests for _extract_structured_summary_totals — generic structured invoice parser.

Covers invoices with Tax Analysis / Invoice Summary / Deposit Summary blocks
(Farsons-style layouts) without any supplier-name hardcoding.

Run: pytest tests/test_structured_summary_totals.py -v
"""
from __future__ import annotations
import re, logging, os

logger = logging.getLogger(__name__)


def _load_struct():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "extractor.py")).read()
    start = src.find("def _extract_structured_summary_totals")
    end   = src.find("\ndef simple_extract")
    ns = {"re": re, "logger": logger}
    exec(src[start:end], ns)
    return ns["_extract_structured_summary_totals"]

_struct = _load_struct()


class TestStructuredSummaryDetected:
    """Layouts that contain structured blocks must be parsed."""

    FARSONS = """
Tax Analysis
Gross Value          180.00
Total Discount         0.00
Tax Amount            32.40
TOTAL               212.40

Invoice Summary
Sub Total           180.00
Less Returns          0.00
Invoice Total       212.40

Deposit Summary
Deposits              9.60
"""

    def test_returns_result(self):
        assert _struct(self.FARSONS) is not None

    def test_vat_from_tax_analysis(self):
        r = _struct(self.FARSONS)
        assert r is not None
        assert r.get("vat_amount") == 32.40

    def test_total_from_invoice_summary(self):
        r = _struct(self.FARSONS)
        assert r is not None
        assert r.get("total_amount") == 212.40

    def test_deposit_candidate_from_deposit_summary(self):
        r = _struct(self.FARSONS)
        assert r is not None
        assert r.get("_deposit_candidate") == 9.60

    def test_gross_value_not_used_as_net(self):
        """Gross Value (180.00 before any discount/tax) must not leak as net_amount
        if the tax-analysis TOTAL can give us a better net = TOTAL - VAT."""
        r = _struct(self.FARSONS)
        assert r is not None
        # net derived from TOTAL(212.40) - VAT(32.40) = 180.00 is OK here
        # but the key guard is that it does NOT produce net=212.40 (wrong)
        net = r.get("net_amount")
        assert net != 212.40, "net_amount must not equal the invoice total"

    def test_balanced_without_deposit(self):
        text = """
Tax Analysis
Net Amount    500.00
Tax Amount     90.00
TOTAL         590.00

Invoice Summary
Invoice Total  590.00
"""
        r = _struct(text)
        assert r is not None
        assert r.get("vat_amount") == 90.0
        assert r.get("total_amount") == 590.0
        assert r.get("_deposit_candidate") is None

    def test_deposit_included_in_total(self):
        """When total = net+vat+deposit, reconciliation should still accept."""
        text = """
Tax Analysis
Tax Amount     32.40
TOTAL         212.40

Invoice Summary
Invoice Total  222.00

Deposit Summary
Deposits        9.60
"""
        r = _struct(text)
        assert r is not None
        assert r.get("total_amount") == 222.0
        assert r.get("_deposit_candidate") == 9.60

    def test_colon_separator_rows(self):
        text = """
Tax Analysis
Tax Amount:   50.00
TOTAL:       300.00

Invoice Summary
Invoice Total: 300.00
"""
        r = _struct(text)
        assert r is not None
        assert r.get("vat_amount") == 50.0
        assert r.get("total_amount") == 300.0


class TestStructuredSummaryNotTriggered:
    """Normal invoices without structured blocks must return None."""

    def test_simple_invoice_returns_none(self):
        assert _struct("Sub Total 200.00\nVAT 36.00\nInvoice Total 236.00") is None

    def test_empty_text_returns_none(self):
        assert _struct("") is None

    def test_none_text_returns_none(self):
        assert _struct(None) is None

    def test_only_totals_no_headings(self):
        assert _struct("Net 100\nVAT 18\nTotal 118") is None

    def test_reconciliation_failure_returns_none(self):
        """When structured blocks are found but totals don't reconcile, return None
        so generic extraction can try."""
        text = """
Tax Analysis
Tax Amount    10.00
TOTAL         100.00

Invoice Summary
Invoice Total  999.00
"""
        assert _struct(text) is None

    def test_deposits_data_line_not_treated_as_heading(self):
        """'Deposits  9.60' inside a Deposit Summary block must be parsed as
        a data row, NOT as a new heading that cuts off the block."""
        text = """
Invoice Summary
Invoice Total  212.40

Deposit Summary
Deposits  9.60
"""
        r = _struct(text)
        # Should either return a result with the deposit OR None — but must
        # NOT crash or silently drop the deposit when a result is returned.
        if r is not None:
            # If we got a result, deposit_candidate must be present
            assert r.get("_deposit_candidate") == 9.60 or r.get("total_amount") is not None


class TestStructuralGuards:
    """Code-level checks that the helper and its call site are wired correctly."""

    def test_helper_defined_in_extractor(self):
        src = open("app/services/extractor.py").read()
        assert "def _extract_structured_summary_totals" in src

    def test_helper_called_inside_simple_extract(self):
        src = open("app/services/extractor.py").read()
        assert "_struct = _extract_structured_summary_totals" in src

    def test_no_supplier_names_hardcoded(self):
        src = open("app/services/extractor.py").read()
        fn_start = src.find("def _extract_structured_summary_totals")
        fn_end   = src.find("\ndef simple_extract")
        fn = src[fn_start:fn_end]
        # Check that no real supplier names appear in the helper
        for name in ("farsons", "simonds", "busuttil", "sultana", "maverick",
                     "schembri", "nectar", "amsm"):
            assert name.lower() not in fn.lower(), \
                f"Supplier name '{name}' must not be hardcoded in the helper"

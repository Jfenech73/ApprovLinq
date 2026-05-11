"""Phase 4 tests — structured totals parsing and learning-readiness metadata.

Covers:
  A. Structured totals parser accuracy (Tax Analysis / Invoice Summary / Deposit Summary)
  B. Source metadata survives the simple_extract path
  C. No supplier names hardcoded anywhere in the structured helper
  D. Reconciliation: prefer coherent net+vat+dep combinations
  E. Learning-ready: _deposit_candidate separated from split decision

Run: pytest tests/test_phase4_structured_and_metadata.py -v
"""
from __future__ import annotations
import os, re, logging

logger = logging.getLogger(__name__)


def _src_extractor() -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", "app", "services", "extractor.py")).read()


def _load_struct():
    src = _src_extractor()
    start = src.find("def _extract_structured_summary_totals")
    end   = src.find("\ndef simple_extract")
    ns = {"re": re, "logger": logger}
    exec(src[start:end], ns)
    return ns["_extract_structured_summary_totals"]


_struct = _load_struct()

_FARSONS_TEXT = """
Tax Analysis
Gross Value          526.22
Total Discount         0.00
Tax Amount            94.72
TOTAL               620.94

Invoice Summary
Sub Total           526.22
Less Returns          0.00
Invoice Total       620.94

Deposit Summary
Deposits             70.80
"""

_SIMPLE_TAX_TEXT = """
Tax Analysis
Net Amount    500.00
Tax Amount     90.00
TOTAL         590.00

Invoice Summary
Invoice Total  590.00
"""

_COLON_TEXT = """
Tax Analysis
Tax Amount:    50.00
TOTAL:        300.00

Invoice Summary
Invoice Total: 300.00
"""


# ── A. Structured totals parser correctness ───────────────────────────────────

class TestStructuredParser:

    def test_tax_analysis_vat_extracted(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert r.get("vat_amount") == 94.72

    def test_invoice_summary_total_extracted(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert r.get("total_amount") == 620.94

    def test_deposit_summary_candidate_extracted(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert r.get("_deposit_candidate") == 70.80

    def test_gross_value_not_used_as_total(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert r.get("total_amount") != 526.22, "Gross Value must not be used as invoice total"

    def test_deposits_not_used_as_net(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert r.get("net_amount") != 70.80, "Deposits must not be used as net_amount"

    def test_balanced_invoice_without_deposit(self):
        r = _struct(_SIMPLE_TAX_TEXT)
        assert r is not None
        assert r.get("vat_amount") == 90.0
        assert r.get("total_amount") == 590.0
        assert r.get("_deposit_candidate") is None

    def test_colon_separator_support(self):
        r = _struct(_COLON_TEXT)
        assert r is not None
        assert r.get("vat_amount") == 50.0
        assert r.get("total_amount") == 300.0

    def test_deposit_candidate_separate_from_split_decision(self):
        """_deposit_candidate is returned but does NOT modify net/vat/total.
        The split decision is deferred to the BCRS detector."""
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        # total_amount must equal the invoice total, not total+deposit
        assert r.get("total_amount") == 620.94
        # net must not include the deposit
        net = r.get("net_amount")
        assert net is None or net != 70.80

    def test_reconciliation_enforced(self):
        """When structured blocks are found but totals don't reconcile, return None."""
        bad = """
Tax Analysis
Tax Amount    10.00
TOTAL         100.00

Invoice Summary
Invoice Total  999.00
"""
        assert _struct(bad) is None

    def test_returns_none_for_plain_invoice(self):
        plain = "Sub Total 200.00\nVAT 36.00\nInvoice Total 236.00"
        assert _struct(plain) is None

    def test_returns_none_for_empty(self):
        assert _struct("") is None
        assert _struct(None) is None


# ── B. simple_extract wiring ──────────────────────────────────────────────────

class TestSimpleExtractWiring:

    def test_struct_helper_called_in_simple_extract(self):
        src = _src_extractor()
        assert "_struct = _extract_structured_summary_totals" in src

    def test_struct_result_applied_to_net(self):
        src = _src_extractor()
        fn_start = src.find("def simple_extract")
        fn_end   = src.find("\ndef suspicious_invoice_number", fn_start)
        fn = src[fn_start:fn_end]
        assert '_struct.get("net_amount")' in fn or "_struct[\"net_amount\"]" in fn

    def test_struct_result_applied_to_vat(self):
        src = _src_extractor()
        fn_start = src.find("def simple_extract")
        fn_end   = src.find("\ndef suspicious_invoice_number", fn_start)
        fn = src[fn_start:fn_end]
        assert '_struct.get("vat_amount")' in fn or "_struct[\"vat_amount\"]" in fn

    def test_struct_result_applied_to_total(self):
        src = _src_extractor()
        fn_start = src.find("def simple_extract")
        fn_end   = src.find("\ndef suspicious_invoice_number", fn_start)
        fn = src[fn_start:fn_end]
        assert '_struct.get("total_amount")' in fn or "_struct[\"total_amount\"]" in fn


# ── C. No hardcoded supplier names ────────────────────────────────────────────

class TestNoHardcodedSuppliers:

    def _get_helper_src(self) -> str:
        src = _src_extractor()
        fn_start = src.find("def _extract_structured_summary_totals")
        fn_end   = src.find("\ndef simple_extract")
        return src[fn_start:fn_end]

    def test_no_farsons(self):
        assert "farsons" not in self._get_helper_src().lower()

    def test_no_nectar(self):
        assert "nectar" not in self._get_helper_src().lower()

    def test_no_sultana(self):
        assert "sultana" not in self._get_helper_src().lower()

    def test_no_amsm(self):
        assert "amsm" not in self._get_helper_src().lower()

    def test_no_maverick(self):
        assert "maverick" not in self._get_helper_src().lower()

    def test_no_schembri(self):
        assert "schembri" not in self._get_helper_src().lower()


# ── D. Reconciliation preference ─────────────────────────────────────────────

class TestReconciliation:

    def test_net_plus_vat_equals_total(self):
        r = _struct(_SIMPLE_TAX_TEXT)
        assert r is not None
        net   = r.get("net_amount")
        vat   = r.get("vat_amount")
        total = r.get("total_amount")
        if net and vat and total:
            assert abs((net + vat) - total) <= 0.10

    def test_net_plus_vat_plus_deposit_accounts_for_total(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        net   = r.get("net_amount")
        vat   = r.get("vat_amount")
        total = r.get("total_amount")
        dep   = r.get("_deposit_candidate")
        if net and vat and total:
            base_ok = abs((net + vat) - total) <= 0.10
            dep_ok  = dep is not None and abs((net + vat + dep) - total) <= 0.10
            assert base_ok or dep_ok, (
                f"net={net} vat={vat} total={total} dep={dep} — "
                "neither net+vat nor net+vat+dep reconciles with total"
            )

    def test_bad_totals_returns_none_not_partial(self):
        """When reconciliation fails, must return None (not a partial result)."""
        text = """
Tax Analysis
Tax Amount    99.00
TOTAL         100.00

Invoice Summary
Invoice Total  500.00
"""
        assert _struct(text) is None


# ── E. Learning metadata readiness ───────────────────────────────────────────

class TestLearningMetadata:
    """Verify the system stores the minimum information for future traceability."""

    def test_deposit_candidate_key_present(self):
        """_deposit_candidate must be a distinct key, not merged into total."""
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        assert "_deposit_candidate" in r

    def test_deposit_candidate_positive(self):
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        dep = r.get("_deposit_candidate")
        assert dep is not None and dep > 0

    def test_total_not_inflated_by_deposit(self):
        """total_amount must be the invoice-as-billed total, not total+deposit."""
        r = _struct(_FARSONS_TEXT)
        assert r is not None
        total = r.get("total_amount")
        dep   = r.get("_deposit_candidate")
        assert total is not None
        if dep:
            assert total != (r.get("net_amount", 0) + r.get("vat_amount", 0) + dep), (
                "total_amount must not include the deposit in the structured result"
            )

    def test_apply_remap_hints_logs_hint_id(self):
        """Hint application must log hint_id for auditability (structural check)."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers", "batches.py")).read()
        hints_fn = src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]
        assert "hint.id" in hints_fn, "hint_id must be logged for auditability"
        assert "source=remap_hint" in hints_fn, "source attribution must be logged"

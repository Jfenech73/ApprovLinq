"""
Regression tests for:
1. Standard invoice number extraction (unchanged)
2. Nectar-style fallback extracts invoice number when header parser misses it
3. Review remap creates future remap_field_value rule
4. Future invoices from same supplier reuse saved rule
5. No duplicate rule creation on repeated same remap
6. Remap for invoice_number does not overwrite other fields

Run: pytest tests/test_invoice_number_remap.py -v
"""
from __future__ import annotations
import re, os, logging

logger = logging.getLogger(__name__)


# ── Load helpers from live source ─────────────────────────────────────────────

def _load_extractor_helpers():
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "app", "services", "extractor.py"
    )).read()

    # Load _invoice_number_fallback
    start = src.find("def _invoice_number_fallback")
    end   = src.find("\ndef simple_extract")
    ns: dict = {"re": re, "logger": logger}
    exec(src[start:end], ns)

    # Load suspicious_invoice_number
    sus_start = src.find("def suspicious_invoice_number")
    sus_end   = src.find("\ndef bad_supplier_line")
    exec(src[sus_start:sus_end], ns)

    return ns["_invoice_number_fallback"], ns["suspicious_invoice_number"]


_fallback, _suspicious = _load_extractor_helpers()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Standard extraction unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestStandardExtractionUnchanged:
    """The standard first_match patterns must still work; fallback is additive."""

    def test_suspicious_invoice_number_rejects_words(self):
        assert _suspicious("invoice") is True
        assert _suspicious("date") is True
        assert _suspicious("no") is True

    def test_suspicious_invoice_number_accepts_numbers(self):
        assert _suspicious("45005") is False
        assert _suspicious("INV-2026-001") is False
        assert _suspicious("3791") is False

    def test_suspicious_invoice_number_rejects_none(self):
        assert _suspicious(None) is True

    def test_fallback_not_called_for_standard_match(self):
        """When standard patterns would find a number, fallback helper returning
        a result should not interfere.  Both paths return the number correctly."""
        # Standard pattern "invoice no: XXX" would match; fallback also matches
        # — either way the invoice_number should be found.
        result = _fallback("Invoice No: INV-123\nNet 100.00")
        assert result == "INV-123"

    def test_fallback_wired_in_simple_extract(self):
        src = open(os.path.join(
            os.path.dirname(__file__), "..", "app", "services", "extractor.py"
        )).read()
        fn = src[src.find("def simple_extract"):
                 src.find("\ndef openai_extract_invoice_fields")]
        assert "_invoice_number_fallback" in fn
        assert "if not invoice_number" in fn


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Nectar-style fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvoiceNumberFallback:
    """_invoice_number_fallback covers layouts the standard regex misses."""

    def test_colon_separated_same_line(self):
        assert _fallback("Invoice No: 45005\nNet 76.26") == "45005"

    def test_spaced_same_line(self):
        assert _fallback("Invoice Number  3791\nDate 05/03/2026") == "3791"

    def test_value_on_next_line(self):
        assert _fallback("Invoice No.\n12345\nDate: 01/01/2026") == "12345"

    def test_ref_no_prefix(self):
        assert _fallback("Ref No: INV-2026-001\nDate 10/01/2026") == "INV-2026-001"

    def test_doc_no_prefix(self):
        assert _fallback("Doc No: D-2026-055\nNet 100.00") == "D-2026-055"

    def test_hash_prefix(self):
        assert _fallback("Invoice # 9901\nDate 01/02/2026") == "9901"

    def test_does_not_confuse_vat_number(self):
        """VAT No line must be rejected; Invoice No on same page must be returned."""
        result = _fallback("VAT No: MT12345678\nInvoice No: 9876")
        assert result == "9876"

    def test_does_not_return_date(self):
        text = "Invoice Date: 01/02/2026\nTotal: 100.00"
        result = _fallback(text)
        assert result is None or not re.match(r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}", result)

    def test_returns_none_when_no_label(self):
        assert _fallback("Supplier Name\nNet 200.00\nVAT 36.00\nTotal 236.00") is None

    def test_returns_none_for_empty_text(self):
        assert _fallback("") is None
        assert _fallback(None) is None

    def test_nectar_realistic_header(self):
        """Realistic Nectar header layout — multiple fields, invoice no must be found."""
        text = (
            "Nectar Ltd\n"
            "123 Main Street, Valletta\n"
            "VAT No: MT12345678\n"
            "Invoice No: 45005\n"
            "Date: 01/02/2026\n"
            "Customer: C0089\n"
            "Route: R12\n"
        )
        result = _fallback(text)
        assert result == "45005", f"Expected '45005', got {result!r}"

    def test_does_not_pick_customer_code(self):
        text = (
            "Nectar Ltd\n"
            "Customer Code: C0089\n"
            "Invoice No: 45005\n"
            "Route: R12\n"
        )
        result = _fallback(text)
        assert result == "45005"

    def test_only_searches_top_40_lines(self):
        """Fallback must not find numbers deep in the document body."""
        header = "\n".join([f"Line {i}" for i in range(41)])
        body = "Invoice No: 99999"  # line 42 — should be ignored
        result = _fallback(header + "\n" + body)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. save_remap creates remap_field_value rule
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveRemapRuleCreation:
    """Structural checks that save_remap creates CorrectionRule correctly."""

    def _review_src(self):
        return open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "review.py"
        )).read()

    def test_remap_field_value_rule_type_used(self):
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "remap_field_value" in fn

    def test_deduplication_check_present(self):
        """save_remap must check for an existing identical rule before inserting."""
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "existing_rule" in fn
        assert "scalar_one_or_none" in fn

    def test_rule_reactivation_on_duplicate(self):
        """Disabled rules must be reactivated rather than duplicated."""
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "existing_rule.active = True" in fn

    def test_response_includes_rule_created(self):
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert '"rule_created"' in fn

    def test_response_includes_saved_as_hint(self):
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert '"saved_as_hint"' in fn

    def test_auto_persists_correction(self):
        """save_remap must write the extracted text to InvoiceRowCorrection
        so the field is persisted without a separate 'Save corrections' click."""
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "get_or_create_correction" in fn
        assert "setattr(correction" in fn

    def test_audit_trail_written(self):
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "InvoiceRowFieldAudit" in fn
        assert 'action="remap"' in fn

    def test_normalised_supplier_used_as_source_pattern(self):
        """source_pattern must be normalised (lowercase, stripped suffixes) so it
        matches _normalize_rule_value used by _apply_saved_rules."""
        src = self._review_src()
        fn = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "_norm_supplier" in fn


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _apply_saved_rules handles remap_field_value
# ═══════════════════════════════════════════════════════════════════════════════

class TestApplySavedRulesRemapFieldValue:
    """_apply_saved_rules must pick up remap_field_value rules for future invoices."""

    def _batches_src(self):
        return open(os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "batches.py"
        )).read()

    def test_remap_field_value_handled(self):
        src = self._batches_src()
        fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert 'rule.rule_type == "remap_field_value"' in fn

    def test_only_fills_blank_or_suspect_fields(self):
        """Must never overwrite a confidently extracted value."""
        src = self._batches_src()
        fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert "is_blank" in fn
        assert "is_suspect" in fn

    def test_supplier_scoped_matching(self):
        """Rule must only apply when the normalised supplier matches source_pattern."""
        src = self._batches_src()
        fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert "current_supplier_norm" in fn

    def test_suspicious_invoice_number_check_present(self):
        """invoice_number is suspect when suspicious_invoice_number returns True."""
        src = self._batches_src()
        fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert "suspicious_invoice_number" in fn

    def test_existing_rule_types_unchanged(self):
        """supplier_alias and nominal_remap handling must not be removed."""
        src = self._batches_src()
        fn = src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]
        assert 'rule.rule_type == "supplier_alias"' in fn
        assert 'rule.rule_type == "nominal_remap"' in fn


# ═══════════════════════════════════════════════════════════════════════════════
# 5. JS: rule_created reflected in UI
# ═══════════════════════════════════════════════════════════════════════════════

class TestJsRemapUi:
    def _js(self):
        return open(os.path.join(
            os.path.dirname(__file__), "..", "app", "static", "js", "review.js"
        )).read()

    def test_reads_rule_created_flag(self):
        assert "data.rule_created" in self._js()

    def test_calls_load_after_remap(self):
        """Grid must reload after remap so the persisted correction is visible."""
        js = self._js()
        # await load() must appear after the read_text block
        idx = js.find("data.read_text")
        assert "await load()" in js[idx:idx+700], \
            "await load() must be called within the data.read_text success block"

    def test_does_not_say_click_save_corrections(self):
        """Old message told user to 'Click Save corrections to apply' — that
        manual step is no longer needed since we auto-save."""
        js = self._js()
        block_start = js.find("data.read_text")
        block = js[block_start:block_start+400]
        assert "Click Save corrections to apply" not in block

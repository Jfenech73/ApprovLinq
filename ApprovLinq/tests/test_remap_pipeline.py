"""
Regression tests for the remap pipeline end-to-end.

Root cause fixed: `logger` was used in `save_remap` (review.py) but never
imported, causing a NameError crash at the first logger.debug() call — AFTER
the hint coordinates were saved but BEFORE the correction was persisted or any
CorrectionRule was created. The DB commit for the correction and rule therefore
never ran, so:
  - the field value was never written to the correction record
  - no future rule was ever created
  - the frontend received no read_text and showed "no text detected"

Additional bugs fixed:
  - RemapIn had no selected_text field so direct UI text selections could
    not be sent to the backend at all
  - No explicit error response when region contained no readable text
  - JS mouseup handler did not capture window.getSelection() or send
    selected_text in the POST body

Run: pytest tests/test_remap_pipeline.py -v
"""
from __future__ import annotations
import ast, re, os


def _src(filename: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", filename)).read()


# ══════════════════════════════════════════════════════════════════════════════
# 1.  The primary crash fix — logger must be defined in review.py
# ══════════════════════════════════════════════════════════════════════════════

class TestLoggerDefined:
    """Without a logger the remap endpoint crashes with NameError before it can
    save anything.  These tests verify the fix is in place."""

    def test_logging_imported(self):
        assert "import logging" in _src("app/routers/review.py"), \
            "review.py must import logging"

    def test_logger_defined_at_module_level(self):
        src = _src("app/routers/review.py")
        assert "logger = logging.getLogger(" in src, \
            "logger must be defined at module level in review.py"

    def test_logger_defined_before_router_and_remap(self):
        src = _src("app/routers/review.py")
        logger_pos = src.find("logger = logging.getLogger(")
        router_pos = src.find("router = APIRouter(")
        remap_pos  = src.find("def save_remap")
        assert logger_pos < router_pos, "logger must be defined before router"
        assert logger_pos < remap_pos,  "logger must be defined before save_remap"

    def test_save_remap_uses_logger(self):
        src = _src("app/routers/review.py")
        fn  = src[src.find("def save_remap"):src.find("\n# ── Rules management")]
        assert "logger.debug(" in fn,   "save_remap must use logger.debug"
        assert "logger.warning(" in fn, "save_remap must use logger.warning for failures"

    def test_review_py_parses(self):
        ast.parse(_src("app/routers/review.py"))   # would raise SyntaxError if broken


# ══════════════════════════════════════════════════════════════════════════════
# 2.  RemapIn schema — selected_text support
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapInSchema:
    """The frontend must be able to send directly selected text so the backend
    can skip the OCR round-trip for digital PDFs."""

    def test_selected_text_field_present(self):
        src = _src("app/routers/review.py")
        model = src[src.find("class RemapIn"):src.find("\ndef _get_batch")]
        assert "selected_text" in model

    def test_selected_text_is_optional_str(self):
        src = _src("app/routers/review.py")
        model = src[src.find("class RemapIn"):src.find("\ndef _get_batch")]
        # Must be Optional (None default) so existing callers without it still work
        assert "None" in model[model.find("selected_text"):]

    def test_apply_as_value_still_present(self):
        src = _src("app/routers/review.py")
        model = src[src.find("class RemapIn"):src.find("\ndef _get_batch")]
        assert "apply_as_value" in model


# ══════════════════════════════════════════════════════════════════════════════
# 3.  save_remap function structure
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveRemapStructure:
    def _fn(self) -> str:
        src = _src("app/routers/review.py")
        return src[src.find("def save_remap"):src.find("\n# ── Rules management")]

    def test_selected_text_prioritised_over_ocr(self):
        fn = self._fn()
        sel_pos = fn.find("payload.selected_text")
        ocr_pos = fn.find("_read_region_text(")
        assert sel_pos < ocr_pos, \
            "selected_text must be checked before falling back to _read_region_text"

    def test_ocr_fallback_still_present(self):
        assert "_read_region_text(" in self._fn()

    def test_explicit_error_on_empty_region(self):
        fn = self._fn()
        assert '"error"' in fn, \
            "save_remap must return an error key when no text can be resolved"

    def test_db_flush_before_text_resolution(self):
        fn = self._fn()
        flush_pos = fn.find("db.flush()")
        # Use the actual _call_ pattern, not the name which appears earlier as a comment
        read_pos  = fn.find("raw = _read_region_text(")
        assert flush_pos > 0, "db.flush() must be present"
        assert read_pos > 0,  "_read_region_text call must be present"
        assert flush_pos < read_pos, \
            "db.flush() must come before _read_region_text() call to get hint.id early"

    def test_exactly_two_commits(self):
        fn = self._fn()
        assert fn.count("db.commit()") == 2, \
            "save_remap must commit exactly twice: once for empty path, once for success"

    def test_correction_auto_persisted(self):
        fn = self._fn()
        assert "get_or_create_correction" in fn
        assert "setattr(correction" in fn

    def test_audit_trail_written(self):
        fn = self._fn()
        assert "InvoiceRowFieldAudit" in fn
        assert 'action="remap"' in fn

    def test_remap_field_value_rule_created(self):
        fn = self._fn()
        assert "remap_field_value" in fn

    def test_rule_deduplication(self):
        fn = self._fn()
        assert "existing_rule" in fn
        assert "scalar_one_or_none" in fn

    def test_rule_reactivation_on_duplicate(self):
        fn = self._fn()
        assert "existing_rule.active = True" in fn

    def test_response_keys(self):
        fn = self._fn()
        for key in ('"id"', '"field_name"', '"page_no"',
                    '"saved_as_hint"', '"rule_created"', '"read_text"'):
            assert key in fn, f"Response must include key {key}"

    def test_normalised_supplier_as_source_pattern(self):
        fn = self._fn()
        assert "_norm" in fn, \
            "supplier name must be normalised before use as source_pattern"


# ══════════════════════════════════════════════════════════════════════════════
# 4.  _apply_saved_rules handles remap_field_value
# ══════════════════════════════════════════════════════════════════════════════

class TestApplySavedRules:
    def _fn(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _apply_saved_rules"):src.find("def _apply_remap_hints")]

    def test_remap_field_value_handled(self):
        assert 'rule.rule_type == "remap_field_value"' in self._fn()

    def test_only_fills_blank_or_suspect(self):
        fn = self._fn()
        assert "is_blank" in fn
        assert "is_suspect" in fn

    def test_supplier_scoped(self):
        assert "current_supplier_norm" in self._fn()

    def test_suspicious_invoice_number_used(self):
        assert "suspicious_invoice_number" in self._fn()

    def test_existing_rule_types_preserved(self):
        fn = self._fn()
        assert 'rule.rule_type == "supplier_alias"' in fn
        assert 'rule.rule_type == "nominal_remap"' in fn


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Frontend JS fixes
# ══════════════════════════════════════════════════════════════════════════════

class TestReviewJsFixes:
    def _js(self) -> str:
        return _src("app/static/js/review.js")

    def test_selected_text_in_payload(self):
        assert "selected_text" in self._js()

    def test_captures_dom_selection(self):
        js = self._js()
        assert "window.getSelection()" in js
        assert "domSel.toString()" in js

    def test_clears_selection_after_capture(self):
        assert "removeAllRanges()" in self._js()

    def test_handles_data_error_key(self):
        assert "data.error" in self._js()

    def test_handles_network_errors(self):
        js = self._js()
        assert "fetchErr" in js or "Network error" in js

    def test_preview_in_confirm_dialog(self):
        assert "previewLabel" in self._js()

    def test_loads_grid_after_success(self):
        js = self._js()
        idx = js.find("data.read_text")
        assert "await load()" in js[idx:idx+700], \
            "await load() must be called within ~700 chars after data.read_text check"

    def test_reads_rule_created(self):
        assert "data.rule_created" in self._js()

    def test_no_manual_save_required(self):
        js = self._js()
        idx = js.find("data.read_text")
        block = js[idx:idx+400]
        assert "Click Save corrections to apply" not in block, \
            "User must not be told to click Save separately — value is auto-saved"


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Invoice number fallback (from previous session, kept for regression)
# ══════════════════════════════════════════════════════════════════════════════

def _load_fallback():
    src = _src("app/services/extractor.py")
    start = src.find("def _invoice_number_fallback")
    end   = src.find("\ndef simple_extract")
    ns: dict = {"re": re}
    import logging
    ns["logger"] = logging.getLogger("test")
    exec(src[start:end], ns)
    return ns["_invoice_number_fallback"]


_fallback = _load_fallback()


class TestInvoiceNumberFallback:
    def test_colon_separated(self):
        assert _fallback("Invoice No: 45005\nNet 76.26") == "45005"

    def test_spaced_same_line(self):
        assert _fallback("Invoice Number  3791\nDate 05/03/2026") == "3791"

    def test_value_on_next_line(self):
        assert _fallback("Invoice No.\n12345\nDate 01/01/2026") == "12345"

    def test_ref_no_prefix(self):
        assert _fallback("Ref No: INV-2026-001") == "INV-2026-001"

    def test_does_not_confuse_vat_number(self):
        result = _fallback("VAT No: MT12345678\nInvoice No: 9876")
        assert result == "9876"

    def test_returns_none_for_empty(self):
        assert _fallback("") is None
        assert _fallback(None) is None

    def test_nectar_header(self):
        text = (
            "Nectar Ltd\nVAT No: MT12345678\n"
            "Invoice No: 45005\nDate: 01/02/2026\nCustomer: C0089\n"
        )
        assert _fallback(text) == "45005"

    def test_wired_into_simple_extract(self):
        src = _src("app/services/extractor.py")
        fn = src[src.find("def simple_extract"):
                 src.find("\ndef openai_extract_invoice_fields")]
        assert "_invoice_number_fallback" in fn
        assert "if not invoice_number" in fn


# ══════════════════════════════════════════════════════════════════════════════
# 7.  BCRS / splitting logic untouched
# ══════════════════════════════════════════════════════════════════════════════

class TestBcrsUntouched:
    """Remap fixes must not affect the stable BCRS/splitting pipeline."""

    def _bcrs_fn(self) -> str:
        src = _src("app/routers/batches.py")
        return src[src.find("def _extract_bcrs_amount_from_summary"):
                   src.find("\ndef _build_bcrs_row")]

    def test_has_label_line_guard_present(self):
        assert "has_label_line" in self._bcrs_fn()

    def test_threshold_still_20(self):
        assert "best_score < 20" in self._bcrs_fn()

    def test_vat_line_rejection_present(self):
        fn = self._bcrs_fn()
        assert "_is_vat_line" in fn

    def test_total_incl_line_rejection_present(self):
        fn = self._bcrs_fn()
        assert "_is_total_incl_line" in fn

    def test_arithmetic_shortcut_absent(self):
        fn = self._bcrs_fn()
        assert "deposit_component_detected shortcut" in fn or \
               "return round(float(m.group(1)), 2)" not in fn

    def test_split_math_preserved(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

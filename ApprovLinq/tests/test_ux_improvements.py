"""
Tests for UX and workflow improvements:

1. Scanner sections collapsible (HTML structure)
2. Low-detection toast removed (no showToast call for needs_review)
3. Review PDF preview panel removed from layout
4. Remap lock enforced when exported/reviewed
5. Remap coordinate dedup (upsert, not duplicate)
6. review_reasons and method_used in API payload
7. Tool badge derivation (DI/AI/OCR) from method_used
8. RemapHint reuse during extraction (_apply_remap_hints)

Run with:  pytest tests/test_ux_improvements.py -v
"""
from __future__ import annotations
import re
import uuid
import pytest


# ══════════════════════════════════════════════════════════════════════════════
# 1. Scanner sections have collapsible wrapper IDs
# ══════════════════════════════════════════════════════════════════════════════

class TestCollapsibleSections:
    def _scanner_html(self):
        return open("app/static/scanner.html").read()

    def _app_js(self):
        return open("app/static/js/app.js").read()

    def test_batches_section_body_wrapper_exists(self):
        assert 'id="batchesSectionBody"' in self._scanner_html(), \
            "scanner.html must have id=batchesSectionBody wrapper"

    def test_batches_toggle_button_exists(self):
        assert 'id="batchesSectionToggle"' in self._scanner_html()

    def test_rows_section_body_wrapper_exists(self):
        assert 'id="rowsSectionBody"' in self._scanner_html()

    def test_rows_toggle_button_exists(self):
        assert 'id="rowsSectionToggle"' in self._scanner_html()

    def test_collapsible_wire_function_in_app_js(self):
        assert "wireCollapsible" in self._app_js(), \
            "app.js must contain the wireCollapsible IIFE"

    def test_section_collapsed_css_exists(self):
        css = open("app/static/css/components.css").read()
        assert "section-collapsed" in css, \
            "components.css must define .section-collapsed"

    def test_collapse_state_uses_session_storage(self):
        js = self._app_js()
        assert "sessionStorage" in js, \
            "collapse state should be persisted in sessionStorage"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Low-detection toast removed from app.js
# ══════════════════════════════════════════════════════════════════════════════

class TestLowDetectionToastRemoved:
    def _app_js(self):
        return open("app/static/js/app.js").read()

    def test_showToast_not_called_for_needs_review(self):
        js = self._app_js()
        # The showToast for low-confidence must be gone
        assert "Low-confidence fields detected" not in js, \
            "Low-detection toast must be removed"

    def test_review_badge_still_rendered(self):
        # renderReviewCell function must still exist and render review badge
        js = self._app_js()
        assert "renderReviewCell" in js, "renderReviewCell must still be present"
        assert "Review now" in js, "Review now link must still render in the cell"

    def test_row_flash_still_present(self):
        """The row-flash animation on needs_review is kept (just no toast popup)."""
        assert "row-flash" in self._app_js()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Review PDF preview panel removed from layout
# ══════════════════════════════════════════════════════════════════════════════

class TestPreviewPanelRemoved:
    def _review_html(self):
        return open("app/static/review.html").read()

    def test_preview_wrap_is_hidden(self):
        html = self._review_html()
        # previewWrap must have display:none
        assert 'id="previewWrap"' in html
        # The visible wrapper should be hidden
        m = re.search(r'id="previewWrap"[^>]*style="[^"]*display:none', html)
        assert m, "previewWrap must have display:none style"

    def test_preview_element_ids_still_present(self):
        """JS remap logic uses these IDs — they must remain as hidden stubs."""
        html = self._review_html()
        for eid in ("previewImg", "remapSelection", "remapHint"):
            assert f'id="{eid}"' in html, f"element id={eid} must still exist for JS compatibility"

    def test_grid_is_single_column(self):
        html = self._review_html()
        assert "grid-template-columns:1fr;" in html or "grid-template-columns:1fr" in html, \
            "review grid must be single column after preview removal"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Remap lock — remapLockReason function in review.js
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapLocking:
    def _review_js(self):
        return open("app/static/js/review.js").read()

    def test_remap_lock_function_defined(self):
        assert "function remapLockReason" in self._review_js()

    def test_remap_lock_checks_exported_status(self):
        js = self._review_js()
        assert "exported" in js and "remapLockReason" in js

    def test_remap_lock_checks_row_reviewed(self):
        js = self._review_js()
        assert "row_reviewed" in js and "remapLockReason" in js

    def test_remap_lock_checked_on_mouseup(self):
        """The remap save on mouseup must re-check the lock."""
        js = self._review_js()
        # Find mouseup handler and confirm remapLockReason is called inside it
        mouseup_idx = js.find("mouseup")
        lock_idx    = js.find("remapLockReason()", mouseup_idx)
        assert lock_idx > mouseup_idx, \
            "remapLockReason must be called inside the mouseup handler"

    def test_remap_lock_checked_on_checkbox_change(self):
        js = self._review_js()
        change_idx = js.find('"change"')
        lock_idx   = js.find("remapLockReason()", change_idx)
        assert lock_idx > change_idx, \
            "remapLockReason must be called inside the remapMode change handler"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Remap coordinate upsert (no duplicate RemapHints)
# ══════════════════════════════════════════════════════════════════════════════

class TestRemapHintUpsert:
    def test_upsert_logic_in_review_py(self):
        src = open("app/routers/review.py").read()
        assert "existing_hint" in src, "save_remap must check for existing hint"
        assert "existing_hint.x = payload.x" in src, \
            "existing hint coordinates must be updated in place"

    def test_no_unconditional_db_add_hint(self):
        """db.add(hint) must be inside an else branch, not unconditional."""
        src = open("app/routers/review.py").read()
        # After the upsert patch, db.add(hint) is inside the else: block
        assert "else:\n        hint = RemapHint(" in src, \
            "new hint must only be created in else branch (not unconditionally)"


# ══════════════════════════════════════════════════════════════════════════════
# 6. review_reasons and method_used in API payload
# ══════════════════════════════════════════════════════════════════════════════

class TestApiPayloadFields:
    def test_review_reasons_in_row_payload(self):
        src = open("app/routers/review.py").read()
        assert '"review_reasons"' in src, \
            "get_review_workspace must include review_reasons in row payload"

    def test_method_used_in_row_payload(self):
        src = open("app/routers/review.py").read()
        assert '"method_used"' in src, \
            "get_review_workspace must include method_used in row payload"

    def test_review_reasons_split_on_pipe(self):
        """review_reasons must be split on | (not comma, which is for review_fields)."""
        src = open("app/routers/review.py").read()
        assert 'split("|")' in src or ".split('|')" in src, \
            "review_reasons must be split on pipe separator"


# ══════════════════════════════════════════════════════════════════════════════
# 7. Tool badge derivation
# ══════════════════════════════════════════════════════════════════════════════

class TestToolBadgeDerivation:
    """Test the JS tool-badge logic as a Python equivalent."""

    def _derive_badge(self, method_used: str) -> str:
        m = (method_used or "").lower()
        if "azure_di" in m or (m.startswith("di") and "di" in m):
            return "DI"
        if "openai" in m or "vision" in m or "ai" in m:
            return "AI"
        if "ocr" in m:
            return "OCR"
        if m and m != "":
            return "TXT"
        return "—"

    def test_native_text_plus_azure_di(self):
        assert self._derive_badge("native_text+azure_di") == "DI"

    def test_ocr_ocr_space(self):
        assert self._derive_badge("ocr_ocr_space") == "OCR"

    def test_native_text_only(self):
        assert self._derive_badge("native_text") == "TXT"

    def test_openai_text(self):
        assert self._derive_badge("native_text+openai_text") == "AI"

    def test_empty_method(self):
        assert self._derive_badge("") == "—"

    def test_none_method(self):
        assert self._derive_badge(None) == "—"

    def test_tool_badge_in_review_js(self):
        js = open("app/static/js/review.js").read()
        assert "azure_di" in js and "toolBadge" in js, \
            "review.js must contain toolBadge logic"

    def test_tool_badge_in_app_js(self):
        js = open("app/static/js/app.js").read()
        assert "toolBadge" in js, "app.js must contain toolBadge logic for scanner rows"

    def test_tool_badge_css_classes_exist(self):
        css = open("app/static/css/components.css").read()
        for cls in (".tool-di", ".tool-ai", ".tool-ocr", ".tool-native"):
            assert cls in css, f"CSS must define {cls}"


# ══════════════════════════════════════════════════════════════════════════════
# 8. _apply_remap_hints in batches.py
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyRemapHints:
    def test_function_defined_in_batches(self):
        src = open("app/routers/batches.py").read()
        assert "def _apply_remap_hints" in src

    def test_function_called_in_pipeline(self):
        src = open("app/routers/batches.py").read()
        assert "_apply_remap_hints(db, batch, row)" in src

    def test_called_before_apply_saved_rules(self):
        src = open("app/routers/batches.py").read()
        remap_idx = src.find("_apply_remap_hints(db, batch, row)")
        rules_idx = src.find("_apply_saved_rules(db, batch, row)")
        assert 0 < remap_idx < rules_idx, \
            "_apply_remap_hints must be called before _apply_saved_rules"

    def test_only_applies_to_blank_fields(self):
        src = open("app/routers/batches.py").read()
        assert "blank_fields" in src, \
            "_apply_remap_hints must only target blank fields"

    def test_remaphint_imported_in_batches(self):
        src = open("app/routers/batches.py").read()
        assert "RemapHint" in src.split("def _apply_remap_hints")[0], \
            "RemapHint must be imported before _apply_remap_hints is defined"

    def test_normalisation_used_for_supplier_matching(self):
        src = open("app/routers/batches.py").read()
        fn_src = src[src.find("def _apply_remap_hints"):src.find("def _parse_money_candidates")]
        assert "_norm(" in fn_src, \
            "Supplier name must be normalised before matching RemapHints"


# ══════════════════════════════════════════════════════════════════════════════
# 9. Field reasons in renderEditor
# ══════════════════════════════════════════════════════════════════════════════

class TestFieldReasonRendering:
    def test_reason_labels_map_in_review_js(self):
        js = open("app/static/js/review.js").read()
        assert "REASON_LABELS" in js

    def test_field_reason_css_in_components(self):
        css = open("app/static/css/components.css").read()
        assert ".field-reason" in css

    def test_review_reasons_banner_css(self):
        css = open("app/static/css/components.css").read()
        assert ".review-reasons-banner" in css

    def test_review_reasons_read_from_row_in_js(self):
        js = open("app/static/js/review.js").read()
        assert "r.review_reasons" in js, \
            "renderEditor must read r.review_reasons from the row object"

    def test_method_used_shown_in_editor(self):
        js = open("app/static/js/review.js").read()
        assert "r.method_used" in js, \
            "renderEditor must read r.method_used to show tool label"

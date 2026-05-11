from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_changed_python_files_parse():
    ast.parse(_read("app/routers/batches.py"))
    ast.parse(_read("app/routers/review.py"))


def test_saved_region_arbitration_helper_present():
    src = _read("app/routers/batches.py")
    assert "def _row_should_arbitrate_with_saved_regions" in src
    assert "azure" in src.lower()
    assert "document[_ -]?intelligence" in src
    assert "confidence_score" in src
    assert "conf < 0.82" in src


def test_di_values_compete_with_saved_regions_before_acceptance():
    src = _read("app/routers/batches.py")
    fn = src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]
    assert "_row_should_arbitrate_with_saved_regions(row)" in fn
    assert "DI/OCR/AI" in fn or "DI" in fn
    assert "target_fields.add(h.field_name)" in fn
    assert "reviewer-approved coordinate" in fn or "saved region" in fn.lower()


def test_strong_existing_values_still_have_guard():
    src = _read("app/routers/batches.py")
    fn = src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]
    assert "strong existing value" in fn
    assert "_should_replace_supplier_with_region" in fn
    assert "continue" in fn


def test_remap_save_never_needs_to_store_blank_supplier_snapshot_when_editor_value_exists():
    src = _read("app/routers/review.py")
    model = src[src.find("class RemapIn"):src.find("def _get_batch")]
    assert "current_value" in model
    fn = src[src.find("def save_remap"):src.find("def apply_saved_regions_to_row")]
    assert "payload.current_value" in fn
    assert "used_current_value_fallback" in fn
    assert "hint.supplier_name_snapshot = read_text" in fn


def test_manual_apply_saved_regions_endpoint_and_ui_present():
    review = _read("app/routers/review.py")
    html = _read("app/static/review.html")
    js = _read("app/static/js/review.js")
    assert "apply_saved_regions_to_row" in review
    assert "/apply-saved-regions" in review
    assert "applySavedRegionsBtn" in html
    assert "Apply saved regions to row" in html
    assert "apply-saved-regions" in js
    assert "Saved regions checked" in js


def test_saved_regions_button_is_maintenance_not_invisible_action():
    html = _read("app/static/review.html")
    js = _read("app/static/js/review.js")
    assert "Manage saved regions" in html
    assert "reviewer-approved coordinates" in html
    assert "Disable" in js and "Delete" in js


def test_frontend_sends_current_field_value_as_ocr_fallback_context():
    js = _read("app/static/js/review.js")
    assert "currentInput" in js
    assert "currentValue" in js
    assert "current_value: currentValue" in js
    assert "used_current_value_fallback" in js

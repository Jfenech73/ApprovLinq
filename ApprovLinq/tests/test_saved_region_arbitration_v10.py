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
    assert "Apply saved rules to row" in html
    assert "apply-saved-regions" in js
    assert "Saved rules checked" in js
    assert "apply_saved_rule_candidates(db, batch, row, candidate_payload=replay_payload)" in review
    assert "resolve_invoice_row(db, batch, row, replay_payload)" in review


def test_saved_rules_button_is_maintenance_not_invisible_action():
    html = _read("app/static/review.html")
    js = _read("app/static/js/review.js")
    assert "Manage saved rules" in html
    assert "reviewer-created field rules and saved regions" in html
    assert "Disable" in js and "Delete" in js


def test_frontend_sends_current_field_value_as_ocr_fallback_context():
    js = _read("app/static/js/review.js")
    assert "currentInput" in js
    assert "currentValue" in js
    assert "current_value: currentValue" in js
    assert "used_current_value_fallback" in js


def test_saved_region_application_is_audited_and_conflicts_mark_review():
    src = _read("app/routers/batches.py")
    fn = src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]
    assert "_audit_saved_region_action" in src
    assert "saved_region_apply" in fn
    assert "saved_region_conflict" in fn
    assert "_append_review_marker" in fn
    assert "remap_hint_conflict" in fn


def test_saved_region_values_are_field_type_validated_before_apply():
    src = _read("app/routers/batches.py")
    assert "def _saved_region_value_is_valid" in src
    assert "def _normalise_saved_region_value" in src
    fn = src[src.find("def _apply_remap_hints"):src.find("def _is_suspect_field_value")]
    assert "_saved_region_value_is_valid(hint.field_name, text)" in fn
    assert "saved_region_invalid" in fn
    assert "field left unchanged" in fn


def test_saved_region_query_is_company_aware():
    src = _read("app/routers/batches.py")
    fn = src[src.find("def _get_active_saved_regions_for_batch"):src.find("def _apply_remap_hints")]
    assert "RemapHint.company_id == batch.company_id" in fn
    assert "RemapHint.company_id.is_(None)" in fn

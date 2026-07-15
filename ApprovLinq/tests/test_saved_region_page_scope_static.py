from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_saved_region_replay_is_anchored_to_current_row_page_not_whole_pdf():
    src = (ROOT / "app/routers/batches.py").read_text()
    assert "def _candidate_pages_for_saved_region" in src
    assert "anchored to the current row" in src
    assert "There is deliberately no" in src and "whole-document search" in src
    assert "add(row_p)" in src
    assert "add(row_p - 1)" in src
    assert "add(row_p + 1)" in src
    assert "abs(hint_p - row_p) <= 1" in src
    assert "for p in range(1, min(max_page, 25) + 1)" not in src


def test_supplier_name_saved_regions_do_not_use_weak_signature_fallback_or_snapshot_on_blank_crop():
    src = (ROOT / "app/routers/batches.py").read_text()
    assert 'h.field_name != "supplier_name" and _signature_overlap' in src
    assert "too weak for identity fields" in src
    assert "_supplier_snapshot_matches_current(getattr(row, \"supplier_name\", None), snap)" in src
    assert "snap and _supplier_hint_candidate_matches_row(row, hint):\n                            text = snap" not in src

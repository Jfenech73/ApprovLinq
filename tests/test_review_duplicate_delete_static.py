from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_review_ui_places_explainability_below_row_list_and_has_delete_block_button():
    js = (ROOT / "app/static/js/review.js").read_text()
    html = (ROOT / "app/static/review.html").read_text()
    assert "selectedExplainPanel" in html
    assert "selectedExplainBody" in html
    assert "row-list-scroll" in html
    assert "renderSelectedExplainPanel()" in js
    assert "renderRowExplainability(r)" in js
    assert "${r.id === state.selected ? renderRowExplainability(r) : \"\"}" not in js
    assert "Transaction details panel below the row list" in js
    assert "Delete / Block Export" in html
    assert "/rows/${state.selected}" in js and "method: \"DELETE\"" in js


def test_duplicate_detection_and_delete_endpoint_exist():
    batches = (ROOT / "app/routers/batches.py").read_text()
    review = (ROOT / "app/routers/review.py").read_text()
    assert "def _mark_duplicate_invoice_rows" in batches
    assert "possible_duplicate_invoice" in batches
    assert "arbitrated:duplicate_check" in batches
    assert "@router.delete(\"/batches/{batch_id}/rows/{row_id}\")" in review
    assert "row_delete_block_export" in review


def test_review_row_selection_preserves_natural_order_and_scroll_position():
    js = (ROOT / "app/static/js/review.js").read_text()
    assert "Selecting a row must not move it" in js
    assert "priorRowScrollTop" in js
    assert "rowScroll.scrollTop = priorRowScrollTop" in js
    assert "visibleRows.sort" not in js
    assert "a.id === state.selected" not in js

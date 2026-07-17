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
    assert "row_soft_block_export" in review
    assert "@router.post(\"/batches/{batch_id}/rows/{row_id}/restore\")" in review
    assert "db.delete(row)" not in review[review.find("def delete_review_row"):review.find("@router.post(\"/batches/{batch_id}/rows/{row_id}/duplicate\")")]


def test_review_row_selection_preserves_natural_order_and_scroll_position():
    js = (ROOT / "app/static/js/review.js").read_text()
    assert "Selecting a row must not move it" in js
    assert "priorRowScrollTop" in js
    assert "rowScroll.scrollTop = priorRowScrollTop" in js
    assert "visibleRows.sort" not in js
    assert "a.id === state.selected" not in js


def test_review_rows_column_is_height_capped_and_scrollable():
    css = (ROOT / "app/static/css/components.css").read_text(encoding="utf-8")
    js = (ROOT / "app/static/js/review.js").read_text()
    assert ".review-3col" in css and "overflow: hidden" in css
    assert ".review-col-rows .row-list-scroll" in css
    assert "max-height: 100%" in css
    assert "overscroll-behavior: contain" in css
    assert ".review-3col{height:100%;max-height:100%;overflow:hidden}" in js
    assert ".row-list-scroll{min-height:0;height:100%;max-height:100%;overflow-y:auto" in js


def test_apply_saved_regions_has_visible_status_feedback():
    js = (ROOT / "app/static/js/review.js").read_text()
    html = (ROOT / "app/static/review.html").read_text()
    assert "applySavedRegionsStatus" in html
    assert "setApplySavedRegionsStatus" in js
    assert "Checking selected row" in js
    assert "Changed:" in js
    assert "Checked; no change" in js
    assert "Conflict:" in js

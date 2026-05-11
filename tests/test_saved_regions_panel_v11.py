from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_review_router_parse_after_saved_region_maintenance_changes():
    ast.parse(_read("app/routers/review.py"))


def test_manage_saved_regions_panel_uses_explicit_visibility_not_hidden_only():
    html = _read("app/static/review.html")
    js = _read("app/static/js/review.js")
    assert 'id="savedRegionsBtn"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="savedRegionsPanel"' in html
    assert 'id="savedRegionsPanel"' in html
    assert 'ap-hidden' in html
    assert 'display:none' in html
    assert "function setSavedRegionsPanelOpen" in js
    assert 'panel.style.display = open ? "flex" : "none"' in js
    assert 'btn.setAttribute("aria-expanded"' in js
    assert 'Hide saved regions' in js


def test_manage_saved_regions_loads_all_regions_and_supports_enable_disable_delete():
    js = _read("app/static/js/review.js")
    review = _read("app/routers/review.py")
    assert "/review/remap-hints?include_inactive=true" in js
    assert 'data-enable-region' in js
    assert 'data-disable-region' in js
    assert 'data-delete-region' in js
    assert 'include_inactive: bool = Query(default=False)' in review
    assert 'if not include_inactive' in review
    assert '@router.post("/remap-hints/{hint_id}/enable")' in review

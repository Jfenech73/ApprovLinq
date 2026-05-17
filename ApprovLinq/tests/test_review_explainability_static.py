from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_review_workspace_returns_explainability_fields():
    text = (ROOT / "app" / "routers" / "review.py").read_text()
    assert "_build_row_explainability" in text
    assert '"explainability": explanation' in text
    assert '"field_evidence": explanation.get("fields", {})' in text
    assert '"totals_reconciliation_status"' in text


def test_audit_endpoint_returns_source_and_confidence():
    text = (ROOT / "app" / "routers" / "review.py").read_text()
    assert '"source": _source_from_action' in text
    assert '"confidence": _extract_confidence' in text
    assert '"explanation": a.note or _human_reason(a.action)' in text


def test_review_page_renders_explainability():
    js = (ROOT / "app" / "static" / "js" / "review.js").read_text()
    assert "renderRowExplainability" in js
    assert "renderFieldEvidence" in js
    assert "field-candidate" in js
    assert "sourceBadge" in js
    assert "totals_reconciliation_status" in js

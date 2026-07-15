from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_azure_di_extract_preserves_raw_structured_fields_before_merge():
    src = read("app/services/extractor.py")
    fn = src[src.index('return {', src.index("def azure_di_extract_invoice(")):src.index("def openai_extract_invoice_fields(", src.index("def azure_di_extract_invoice("))]
    assert '"_di_structured_fields": {' in fn
    assert '"supplier_name": supplier_name,' in fn
    assert '"invoice_number": invoice_number,' in fn
    assert '"total_amount": total_amount,' in fn


def test_merge_ai_fields_marks_field_level_provenance_and_keeps_di_structured_payload():
    src = read("app/services/extractor.py")
    fn = src[src.index("def merge_ai_fields("):src.index("def process_pdf_page(", src.index("def merge_ai_fields("))]
    assert 'field_sources = dict(merged.get("_field_sources") or {})' in fn
    assert '"_di_structured_fields"' in fn
    assert 'merged["_field_sources"] = field_sources' in fn
    assert '"azure_di_structured"' in fn


def test_simple_extract_uses_resolved_currency_not_undefined_currency_local():
    src = read("app/services/extractor.py")
    fn = src[src.index("def simple_extract("):src.index("def _call_openai(", src.index("def simple_extract("))]
    assert "resolved_currency = _extract_currency_code(text)" in fn
    assert 'if resolved_currency and "currency" not in _field_sources:' in fn
    assert '"currency": resolved_currency,' in fn


def test_extractor_has_current_build_tag_log_marker():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"' in src
    assert "process_pdf_page build=%s page=%d file=%s" in src


def test_arbitration_uses_field_level_source_type_and_adds_raw_di_candidates():
    src = read("app/services/invoice_arbitration.py")
    assert "def _field_source_type(" in src
    assert '"azure_di_structured"' in src
    assert "def _raw_candidates(" in src
    assert "source_type=field_source_type" in src


def test_export_di_summary_uses_azure_di_structured_candidates_too():
    src = read("app/services/corrected_exporter.py")
    assert 'InvoiceFieldCandidate.source_type.in_(["azure_di", "azure_di_structured"])' in src


def test_review_labels_include_azure_di_structured_and_header_rule():
    src = read("app/routers/review.py")
    assert '"header_rule": "Header rule"' in src
    assert '"azure_di_structured": "Azure DI structured"' in src

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_simple_extract_uses_header_focused_identity_reads_and_supplier_vat():
    src = read("app/services/extractor.py")
    fn = src[src.index("def simple_extract("):src.index("def _call_openai(", src.index("def simple_extract("))]
    assert "header_text = _header_region_text(text, max_lines=36)" in fn
    assert "identity_text = header_text or clean_text(text)" in fn
    assert "_collect_invoice_number_candidates(identity_text)" in fn
    assert "_invoice_number_fallback(identity_text)" in fn
    assert "invoice_date_raw = _extract_invoice_date_value(identity_text)" in fn
    assert 'supplier_vat = _extract_supplier_vat_number(identity_text) or _extract_supplier_vat_number(text)' in fn
    assert '"supplier_vat": supplier_vat' in fn


def test_review_evidence_prefers_filtered_header_and_totals_views():
    src = read("app/services/extractor.py")
    fn = src[src.index("def process_pdf_page("):src.index("def process_pdf_page_rows(", src.index("def process_pdf_page("))]
    assert '_header_view = extracted.get("_header_text") or ""' in fn
    assert '_totals_view = extracted.get("_totals_text") or ""' in fn
    assert "header_raw = extracted.get(\"_header_text\")" in fn
    assert "totals_raw = extracted.get(\"_totals_text\")" in fn
    assert "_filtered_di_totals = _totals_region_text(_di_text, tail_lines=18)" in fn


def test_process_pdf_page_backfills_currency_from_filtered_text_views():
    src = read("app/services/extractor.py")
    fn = src[src.index("def process_pdf_page("):src.index("def process_pdf_page_rows(", src.index("def process_pdf_page("))]
    assert "if not extracted.get(\"currency\"):" in fn
    assert "_extract_currency_code(extracted.get(\"_header_text\"))" in fn
    assert "_extract_currency_code(extracted.get(\"_totals_text\"))" in fn

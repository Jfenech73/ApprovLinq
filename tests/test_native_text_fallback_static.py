from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "app" / "services" / "extractor.py"


def _src() -> str:
    return EXTRACTOR.read_text(encoding="utf-8")


def test_native_text_is_scored_by_invoice_signals_not_length_only():
    src = _src()
    assert "def _invoice_text_signal_score" in src
    assert "def _native_text_looks_usable" in src
    assert "count_meaningful_chars(text or \"\") >= 80" in src
    assert "_invoice_text_signal_score(text) >= 3" in src


def test_weak_native_text_triggers_ocr_before_extraction():
    src = _src()
    assert "native_text_rejected" in src
    assert "_get_fallback_ocr_text(pdf_path, page_index, native_text)" in src
    assert "not _native_text_looks_usable(native_text)" in src


def test_blank_or_weak_native_result_gets_second_chance_ocr():
    src = _src()
    assert "native_text_weak_result" in src
    assert "not _extraction_has_minimum_invoice_fields(extracted)" in src
    assert "ocr_extracted = simple_extract" in src


def test_vision_and_validation_receive_best_available_text():
    src = _src()
    assert "openai_extract_invoice_vision(\n                jpeg_b64,\n                final_text," in src
    assert "openai_validate_extraction(\n                final_text or native_text," in src

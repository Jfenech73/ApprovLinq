from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "app" / "services" / "extractor.py"


def _src() -> str:
    return EXTRACTOR.read_text(encoding="utf-8")


def test_native_text_is_not_primary_extraction_route():
    src = _src()
    assert "Native PDF text is intentionally NOT used as the primary extraction text" in src
    assert "method = \"ocr_primary\"" in src
    assert "method = \"native_text\"" not in src


def test_ocr_is_attempted_before_simple_extract_baseline():
    src = _src()
    ocr_pos = src.index("ocr_text, ocr_method = _get_fallback_ocr_text(pdf_path, page_index, native_text)")
    simple_pos = src.index("extracted = simple_extract(", ocr_pos)
    assert ocr_pos < simple_pos


def test_native_text_is_not_used_as_text_only_ai_fallback():
    src = _src()
    assert '_text_for_ai = final_text if count_meaningful_chars(final_text) >= 20 else ""' in src
    assert "else native_text" not in src


def test_validation_receives_final_text_not_native_text_fallback():
    src = _src()
    assert "openai_validate_extraction(\n                final_text,\n                extracted," in src
    assert "final_text or native_text" not in src


def test_ocr_unavailable_drives_image_based_fallbacks_not_native_extraction():
    src = _src()
    assert "ocr_unavailable_native_text_ignored" in src
    assert "Do not fall back to native text for field extraction" in src

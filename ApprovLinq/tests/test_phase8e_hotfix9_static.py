from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_config_exposes_provider_baseline_mode():
    src = read("app/config.py")
    assert "scan_provider_baseline_mode: bool = False" in src
    assert '"scan_provider_baseline_mode": self.scan_provider_baseline_mode,' in src


def test_extractor_has_provider_baseline_result_helper_and_build_tag():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"' in src
    assert "def _build_provider_baseline_result(" in src
    assert '"validation_status": "review_provider_baseline" if missing_core else "ok_provider_baseline",' in src
    assert '"supplier_match_method": "provider_direct",' in src


def test_process_pdf_page_provider_baseline_mode_disables_openai_and_returns_early():
    src = read("app/services/extractor.py")
    fn = src[src.index("def process_pdf_page("):src.index("def _line_amount_from_text", src.index("def process_pdf_page("))]
    assert 'provider_baseline_mode = bool(getattr(settings, "scan_provider_baseline_mode", False))' in fn
    assert "not provider_baseline_mode" in fn
    assert "return _build_provider_baseline_result(" in fn


def test_batches_skip_post_processing_in_provider_baseline_mode():
    src = read("app/routers/batches.py")
    assert 'provider_baseline_mode = bool(getattr(settings, "scan_provider_baseline_mode", False))' in src
    assert 'if not provider_baseline_mode:' in src
    assert '_append_method_tag(row, "provider_baseline_mode")' in src
    assert "detect_within_batch_duplicates(db, batch_id, scan_run_id)" in src

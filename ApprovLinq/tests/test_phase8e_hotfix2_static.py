from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_process_pdf_page_gates_provider_calls_and_reuses_rendered_image():
    src = read("app/services/extractor.py")
    fn = src[src.index("def process_pdf_page("):src.index("def _line_amount_from_text", src.index("def process_pdf_page("))]
    assert "use_azure_di = _di_ok and not skip_azure_di" in fn
    assert "use_azure_di_read_fallback" in fn
    assert "if use_azure_di or use_azure_di_read_fallback or use_vision:" in fn
    assert fn.count("OCRBackend.render_pdf_page_to_jpeg_bytes(") == 2


def test_supplier_identity_conflict_blocks_later_fuzzy_account_canonicalisation():
    src = read("app/routers/batches.py")
    assert "supplier_identity_conflicted" in src
    assert "supplier_resolver:account_match_skipped_ambiguous" in src


def test_supplier_identity_resolution_is_audited_as_evidence():
    src = read("app/routers/batches.py")
    assert "def _audit_supplier_identity_resolution" in src
    assert "supplier_identity_apply" in src
    assert "supplier_identity_conflict" in src


def test_apply_saved_regions_returns_region_event_diagnostics():
    src = read("app/routers/review.py")
    assert "region_events.append" in src
    assert '"region_events": region_events' in src
    assert '"checked_regions": len(region_events)' in src


def test_rule_audit_defines_old_and_new_text_before_insert():
    src = read("app/routers/batches.py")
    fn = src[src.index("def _audit_rule_application"):src.index("def _audit_supplier_identity_resolution")]
    assert "old_text = None if old_value is None else str(old_value)" in fn
    assert "new_text = None if new_value is None else str(new_value)" in fn

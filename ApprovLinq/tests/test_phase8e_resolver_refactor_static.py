from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_di_is_attempted_before_ocr_fallback_and_candidates_are_labelled():
    src = read("app/services/extractor.py")
    assert "Azure DI before OCR where safe" in src
    assert "OCR fallback remains available" in src
    assert 'method = "azure_di"' in src
    assert '"extraction_source"' in src
    arb = read("app/services/invoice_arbitration.py")
    assert '"azure_di"' in arb
    assert '"ocr_space"' in arb
    assert '"native_text"' in arb


def test_selective_arbitration_keeps_invoice_number_and_date_validation_only():
    src = read("app/services/invoice_arbitration.py")
    assert "STRONG_RESOLUTION_FIELDS" in src
    assert "LIGHT_VALIDATION_FIELDS" in src
    assert "invoice_number" in src and "invoice_date" in src
    assert "_candidate_allowed_for_light_validation" in src


def test_supplier_identity_resolver_runs_before_saved_region_candidate_arbitration():
    src = read("app/routers/batches.py")
    assert "def _resolve_supplier_identity" in src
    assert "supplier_identity:vat_exact" in src
    assert "supplier_identity_conflict:fuzzy_ambiguous" in src
    worker = src[src.index("def _process_batch_job"):]
    assert "resolve_supplier_identity(" in worker
    assert "_apply_remap_hints(db, batch, row" in src
    assert "Conservative supplier-name hint match that does not require current supplier to be correct" in src


def test_saved_regions_are_reported_as_candidate_style_diagnostics():
    batches = read("app/routers/batches.py")
    review = read("app/routers/review.py")
    js = read("app/static/js/review.js")
    assert "source=remap_hint" in batches
    assert "saved_region_blank" in batches
    assert "saved_region_invalid" in batches
    assert "saved_region_checked" in batches
    assert "saved_region_conflict" in batches
    assert '"diagnostics": saved_region_diagnostics' in review
    assert "checkedFields" in js


def test_correction_rules_do_not_replay_dynamic_field_values_broadly():
    batches = read("app/routers/batches.py")
    arb = read("app/services/invoice_arbitration.py")
    assert 'field in {"invoice_number", "invoice_date"} and rule.rule_type == "remap_field_value"' in batches
    assert 'field_name in {"invoice_number", "invoice_date"} and rule.rule_type not in {"text_correction"}' in arb
    assert 'rule.rule_type == "text_correction" and not field_match' in arb

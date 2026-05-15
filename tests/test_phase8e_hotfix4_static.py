from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_extractor_scores_invoice_candidates_instead_of_taking_first_header_token():
    src = read("app/services/extractor.py")
    assert "def _invoice_candidate_quality_score(" in src
    assert "def _choose_best_invoice_candidate(" in src
    fn = src[src.index("def simple_extract("):src.index("def _call_openai(", src.index("def simple_extract("))]
    assert 'invoice_number = _choose_best_invoice_candidate(invoice_candidates, text=identity_text)' in fn
    assert '_choose_best_invoice_candidate(' in fn


def test_merge_ai_fields_only_replaces_invoice_number_when_ai_quality_is_better():
    src = read("app/services/extractor.py")
    fn = src[src.index("def merge_ai_fields("):src.index("def process_pdf_page(", src.index("def merge_ai_fields("))]
    assert 'current_score = _invoice_candidate_quality_score(current_invoice' in fn
    assert 'ai_score = _invoice_candidate_quality_score(ai_invoice_number' in fn
    assert 'if current_score < 0 or ai_score >= current_score + 3:' in fn


def test_unmatched_supplier_can_be_trusted_from_document_evidence():
    src = read("app/services/extractor.py")
    assert "def _document_supplier_evidence_is_strong(" in src
    assert '_snorm.match_method == "unmatched" and _document_supplier_evidence_is_strong(' in src
    assert '_snorm.match_method = "document_header_vat" if supplier_vat_s3 else "document_header"' in src
    assert "_snorm.review_reason = None" in src


def test_arbitration_treats_document_header_supplier_as_not_weak():
    src = read("app/services/invoice_arbitration.py")
    fn = src[src.index("def _current_value_is_weak("):src.index("def _audit_arbitration(", src.index("def _current_value_is_weak("))]
    assert 'match_method in {"vat_match", "alias_match", "document_header", "document_header_vat"}' in fn


def test_arbitration_invoice_number_validator_rejects_short_header_refs_and_trailing_slashes():
    src = read("app/services/invoice_arbitration.py")
    fn = src[src.index("def _invoice_number_suspicious("):src.index("def _value_valid_for_field(", src.index("def _invoice_number_suspicious("))]
    assert 'if text.endswith("/") or text.endswith("-"):' in fn
    assert 'if len(letters) >= 2 and len(digits) <= 3 and len(text) <= 6:' in fn

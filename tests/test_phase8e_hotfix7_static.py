from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_supplier_resolver_helpers_exist():
    src = read("app/services/extractor.py")
    assert "def _supplier_identity_tokens(value: str | None) -> tuple[str, ...]:" in src
    assert "def _supplier_candidates_similar(a: str | None, b: str | None) -> bool:" in src
    assert "def _resolve_supplier_identity(" in src
    assert 'SequenceMatcher(None, aa.lower(), bb.lower()).ratio() >= 0.84' in src


def test_merge_ai_fields_uses_supplier_identity_resolver():
    src = read("app/services/extractor.py")
    fn = src[src.index("def merge_ai_fields("):src.index("def process_pdf_page(", src.index("def merge_ai_fields("))]
    assert "resolved_supplier, resolved_source, supplier_meta = _resolve_supplier_identity(" in fn
    assert 'field_sources["supplier_name"] = resolved_source or (' in fn
    assert 'merged["_supplier_resolution"] = supplier_meta' in fn


def test_unmatched_but_strong_supplier_can_promote_to_document_header():
    src = read("app/services/extractor.py")
    fn = src[src.index("# 3c"):src.index("# 3d", src.index("# 3c"))]
    assert 'supplier_source in {"header_supplier", "header_contact", "header_candidate", "azure_di_structured"}' in fn
    assert 'int(supplier_resolution.get("score") or 0) >= 18' in fn
    assert 'int(supplier_resolution.get("support") or 0) >= 1' in fn
    assert '_snorm.match_method = "document_header_vat" if supplier_vat_s3 else "document_header"' in fn


def test_document_supplier_evidence_thresholds_are_relaxed_for_plausible_headers():
    src = read("app/services/extractor.py")
    fn = src[src.index("def _document_supplier_evidence_is_strong("):src.index("def bad_supplier_line(", src.index("def _document_supplier_evidence_is_strong("))]
    assert "if score >= 8:" in fn
    assert "return score >= 6 and supplier_conf >= 0.50" in fn

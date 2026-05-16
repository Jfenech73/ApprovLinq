from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_supplier_resolver_requires_actionable_evidence():
    src = read("app/services/extractor.py")
    assert "def _supplier_candidate_is_actionable(candidate: dict[str, Any], supplier_vat: str | None = None) -> bool:" in src
    assert 'if norm.match_method == "vat_match":' in src
    assert 'if source in {"header_supplier", "azure_di_structured"} and support >= 1 and final_score >= 24:' in src
    assert 'if not _supplier_candidate_is_actionable(best, supplier_vat=supplier_vat):' in src
    assert 'return None, None, {"support": best["support"], "score": best["final_score"], "source": best["representative_source"]}' in src


def test_simple_extract_supplier_candidates_are_header_only():
    src = read("app/services/extractor.py")
    fn = src[src.index("def simple_extract("):src.index("def _call_openai(", src.index("def simple_extract("))]
    assert "supplier_header_text = _header_region_text(text, max_lines=18)" in fn
    assert '_collect_supplier_candidates(supplier_header_text or identity_text, account_tokens=account_tokens)' in fn
    assert '_collect_supplier_candidates(text, account_tokens=account_tokens)' not in fn
    assert '"_supplier_header_text": supplier_header_text,' in fn


def test_merge_ai_fields_leaves_supplier_unresolved_when_not_actionable():
    src = read("app/services/extractor.py")
    fn = src[src.index("def merge_ai_fields("):src.index("def process_pdf_page(", src.index("def merge_ai_fields("))]
    assert 'merged["supplier_name"] = None' in fn
    assert 'merged["_supplier_resolution"] = supplier_meta' in fn


def test_hotfix8_supplier_guards_still_present_under_current_build():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10a"' in src

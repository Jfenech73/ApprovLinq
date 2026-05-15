from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_raw_candidates_persist_richer_azure_di_evidence():
    src = read("app/services/invoice_arbitration.py")
    assert "def _raw_candidate_evidence(" in src
    assert 'parts = [f"provider=azure_di", f"method={method}"]' in src
    assert 'parts.append(f"vendor_tax_id={payload.get(\'supplier_vat\')}")' in src
    assert 'parts.append(f"header_excerpt={excerpt}")' in src
    assert 'parts.append(f"totals_excerpt={excerpt}")' in src
    assert "evidence=_raw_candidate_evidence(field_name, payload, source_type)" in src


def test_corrected_exporter_builds_di_candidate_summary_from_persisted_candidates():
    src = read("app/services/corrected_exporter.py")
    assert "def _build_di_candidate_summary_map" in src
    assert 'InvoiceFieldCandidate.source_type == "azure_di"' in src
    assert 'd["di_candidate_summary"] = di_summary_by_row.get(r.id)' in src


def test_export_evidence_sheet_includes_di_candidate_summary():
    src = read("app/services/exporter.py")
    assert '"di_candidate_summary"' in src

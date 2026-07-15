from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_extractor_persists_raw_di_payload_and_extended_header_fields():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"' in src
    assert "di_raw_payload = _serialise_di_document(result)" in src
    assert '"_di_raw_fields": di_raw_fields,' in src
    assert '"_di_raw_payload": di_raw_payload,' in src
    assert '"raw_di_document_confidence": getattr(document, "confidence", None),' in src
    assert '"customer_address": customer_addr,' in src


def test_models_define_invoice_read_header_and_detail_tables():
    src = read("app/db/models.py")
    assert "class InvoiceReadHeader(Base):" in src
    assert '__tablename__ = "invoice_read_headers"' in src
    assert "class InvoiceReadDetail(Base):" in src
    assert '__tablename__ = "invoice_read_details"' in src


def test_migration_creates_invoice_read_header_and_detail_tables():
    src = read("alembic/versions/2026_05_16_0003_invoice_read_tables.py")
    assert '"invoice_read_headers"' in src
    assert '"invoice_read_details"' in src
    assert '"raw_di_fields"' in src
    assert '"raw_di_payload"' in src


def test_batch_persists_read_snapshot_during_scan():
    src = read("app/routers/batches.py")
    assert "def _persist_invoice_read_snapshot(" in src
    assert "_persist_invoice_read_snapshot(" in src[src.index("def _process_batch_job"):]
    assert "raw_provider_fields=_json_safe(payload.get(\"_di_structured_fields\") or {})," in src
    assert "raw_provider_payload=_json_safe({" in src
    assert "raw_di_fields = payload.get(\"_di_raw_fields\") or {}" in src

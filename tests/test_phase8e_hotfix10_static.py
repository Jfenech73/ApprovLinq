from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_extractor_persists_raw_di_payload_and_extended_header_fields():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10b"' in src
    assert "from datetime import date, datetime" in src
    assert 'supplier_addr_recipient, _ = _str(fields.get("VendorAddressRecipient"))' in src
    assert 'customer_addr_recipient, _ = _str(fields.get("CustomerAddressRecipient"))' in src
    assert 'order_number, _           = _str(fields.get("OrderNumber"))' in src
    assert 'purchase_order, _         = _str(fields.get("PurchaseOrder"))' in src
    assert 'di_raw_payload = _serialise_di_document(result)' in src
    assert '"_di_raw_fields": di_raw_fields,' in src
    assert '"_di_raw_payload": di_raw_payload,' in src
    assert '"raw_di_document_confidence": getattr(document, "confidence", None),' in src
    assert '"customer_address": customer_addr,' in src


def test_models_define_invoice_read_header_and_detail_tables():
    src = read("app/db/models.py")
    assert 'class InvoiceReadHeader(Base):' in src
    assert '__tablename__ = "invoice_read_headers"' in src
    assert 'raw_di_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)' in src
    assert 'raw_di_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)' in src
    assert 'class InvoiceReadDetail(Base):' in src
    assert '__tablename__ = "invoice_read_details"' in src
    assert 'header_id: Mapped[int] = mapped_column(ForeignKey("invoice_read_headers.id", ondelete="CASCADE"), nullable=False)' in src


def test_migration_creates_invoice_read_header_and_detail_tables():
    src = read("alembic/versions/2026_05_16_0003_invoice_read_tables.py")
    assert '"invoice_read_headers"' in src
    assert 'sa.Column("supplier_address_recipient", sa.Text(), nullable=True),' in src
    assert 'sa.Column("order_number", sa.String(120), nullable=True),' in src
    assert 'sa.Column("purchase_order", sa.String(120), nullable=True),' in src
    assert 'sa.Column("raw_di_fields", sa.JSON(), nullable=True),' in src
    assert 'sa.Column("raw_di_payload", sa.JSON(), nullable=True),' in src
    assert '"invoice_read_details"' in src
    assert 'sa.Column("raw_detail", sa.JSON(), nullable=True),' in src


def test_batch_persists_read_snapshot_before_downstream_mutation():
    src = read("app/routers/batches.py")
    assert "def _persist_invoice_read_snapshot(" in src
    assert "db.flush()\n                            _persist_invoice_read_snapshot(" in src
    assert 'raw_di_fields=_json_safe(payload.get("_di_raw_fields") or {}),' in src
    assert 'raw_di_payload=_json_safe(payload.get("_di_raw_payload") or {}),' in src

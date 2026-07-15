from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_extractor_serialises_di_arrays_and_objects_and_build_tag():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"' in src
    assert "def _di_field_content_text(field: Any) -> str | None:" in src
    assert "def _di_direct_field_value(field: Any) -> Any:" in src
    assert "def _build_direct_di_page_rows(" in src
    assert "def _apply_direct_blank_field_fallbacks(" in src
    assert "filled_blank_after_direct_di" in src
    assert '"extraction_source": "azure_di_direct",' in src
    assert "payload[\"value_array\"]" in src
    assert 'payload["value_object"]' in src


def test_models_include_microsoft_named_header_and_prefixed_detail_columns():
    src = read("app/db/models.py")
    for marker in [
        'VendorName: Mapped[str | None] = mapped_column("VendorName", Text, nullable=True)',
        'InvoiceId: Mapped[str | None] = mapped_column("InvoiceId", Text, nullable=True)',
        'PaymentDetails: Mapped[dict | list | None] = mapped_column("PaymentDetails", JSON, nullable=True)',
        'di_amount: Mapped[str | None] = mapped_column(Text, nullable=True)',
    ]:
        assert marker in src


def test_batches_persist_microsoft_named_fields_from_raw_di():
    src = read("app/routers/batches.py")
    assert "def _di_field_scalar_value(" in src
    assert "def _di_field_complex_value(" in src
    assert 'VendorName=_di_field_scalar_value(raw_di_fields.get("VendorName")),' in src
    assert 'PaymentDetails=_di_field_complex_value(raw_di_fields.get("PaymentDetails")),' in src
    assert "def _persist_selected_field_candidates(" in src
    assert "selected_from_fallback_provider" in src


def test_migrations_add_microsoft_named_headers_and_phase5_prefixed_details():
    named_src = read("alembic/versions/2026_05_16_0004_invoice_read_di_named_columns.py")
    phase5_src = read("alembic/versions/2026_07_10_0009_database_migration_foundation.py")
    assert 'revision = "20260516_0004"' in named_src
    assert 'sa.Column("VendorName", sa.Text(), nullable=True),' in named_src
    assert 'revision = "20260710_0009"' in phase5_src
    assert '"di_amount": sa.Column("di_amount", sa.Text(), nullable=True),' in phase5_src

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_extractor_serialises_di_arrays_and_objects_and_build_tag():
    src = read("app/services/extractor.py")
    assert 'EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"' in src
    assert '"%d-%b-%Y"' in src
    assert "total_amount = round(float(net_amount) + float(vat_amount), 2)" in src
    assert "def _di_field_content_text(field: Any) -> str | None:" in src
    assert "def _di_direct_field_value(field: Any) -> Any:" in src
    assert "def _build_direct_di_page_rows(" in src
    assert "def _apply_direct_blank_field_fallbacks(" in src
    assert "filled_blank_after_direct_di" in src
    assert "fallback_used_for_critical_field" in src
    assert '"extraction_source": "azure_di_direct",' in src
    assert '"review_required": False,' in src
    assert 'supplier_name = _clean_di_supplier_name(_di_field_content_text(fields.get("VendorName")) or supplier_name)' in src
    assert 'raw_vendor_name = _di_field_content_text(raw_di_fields.get("VendorName"))' in src
    assert 'payload["value_array"] = [_serialise_di_field(item) for item in value_array]' in src
    assert 'payload["value_object"] = {' in src


def test_models_include_microsoft_named_header_and_prefixed_detail_columns():
    src = read("app/db/models.py")
    for marker in [
        'BatchPages: Mapped[int | None] = mapped_column("BatchPages", Integer, nullable=True)',
        'VendorName: Mapped[str | None] = mapped_column("VendorName", Text, nullable=True)',
        'InvoiceId: Mapped[str | None] = mapped_column("InvoiceId", Text, nullable=True)',
        'PaymentDetails: Mapped[dict | list | None] = mapped_column("PaymentDetails", JSON, nullable=True)',
        'di_amount: Mapped[str | None] = mapped_column(Text, nullable=True)',
        'di_product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)',
        'di_unit_price: Mapped[str | None] = mapped_column(Text, nullable=True)',
    ]:
        assert marker in src


def test_batches_persist_microsoft_named_fields_from_raw_di():
    src = read("app/routers/batches.py")
    assert "def _di_field_scalar_value(" in src
    assert "def _di_field_complex_value(" in src
    assert "def _existing_table_columns(" in src
    assert "def _filter_existing_columns(" in src
    assert 'VendorName=_di_field_scalar_value(raw_di_fields.get("VendorName")),' in src
    assert 'PaymentDetails=_di_field_complex_value(raw_di_fields.get("PaymentDetails")),' in src
    assert 'raw_items = _di_field_complex_value(raw_di_fields.get("Items"))' in src
    assert 'di_amount=_di_field_scalar_value(item_fields.get("Amount")),' in src
    assert 'InvoiceReadHeader(**_filter_existing_columns(db, "invoice_read_headers", header_values))' in src
    assert 'InvoiceReadDetail(**_filter_existing_columns(db, "invoice_read_details", detail_values))' in src
    assert "def _persist_selected_field_candidates(" in src
    assert "def _apply_blank_saved_regions_as_candidates(" in src
    assert "filled_blank_from_saved_region" in src
    assert "def _apply_blank_field_stable_rules(" in src
    assert "filled_blank_from_stable_identifier_rule" in src
    assert "selected_from_fallback_provider" in src


def test_migrations_add_microsoft_named_headers_and_phase5_prefixed_details():
    named_src = read("alembic/versions/2026_05_16_0004_invoice_read_di_named_columns.py")
    phase5_src = read("alembic/versions/2026_07_10_0009_database_migration_foundation.py")
    assert 'revision = "20260516_0004"' in named_src
    assert 'sa.Column("VendorName", sa.Text(), nullable=True),' in named_src
    assert 'sa.Column("PaymentDetails", sa.JSON(), nullable=True),' in named_src
    assert 'revision = "20260710_0009"' in phase5_src
    assert '"di_amount": sa.Column("di_amount", sa.Text(), nullable=True),' in phase5_src

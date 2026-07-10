from pathlib import Path


def test_invoice_row_review_fields_are_text_and_startup_alters_are_present():
    models = Path("app/db/models.py").read_text()
    migration = Path("alembic/versions/2026_07_10_0009_database_migration_foundation.py").read_text()
    assert "review_reasons: Mapped[str | None] = mapped_column(Text" in models
    assert "review_fields: Mapped[str | None] = mapped_column(Text" in models
    assert '_alter_column_type_if_present("invoice_rows", "method_used", sa.Text())' in migration
    assert '_alter_column_type_if_present("invoice_rows", "review_reasons", sa.Text())' in migration
    assert '_alter_column_type_if_present("invoice_rows", "review_fields", sa.Text())' in migration


def test_scan_operational_logging_is_present_and_content_safe():
    batches = Path("app/routers/batches.py").read_text()
    assert "def _safe_log_value" in batches
    assert "scan page completed batch=%s" in batches
    assert "scan page failed batch=%s" in batches
    assert "scan completed batch=%s status=%s" in batches
    assert "rules_applied=%d" in batches
    assert "saved_regions_applied=%d" in batches
    assert "totals_status=%s" in batches
    assert "extraction_methods=%s" in batches


def test_export_operational_logging_is_present():
    exporter = Path("app/services/corrected_exporter.py").read_text()
    assert "logger = logging.getLogger(__name__)" in exporter
    assert "export completed batch=%s version=%d rows=%d" in exporter


def test_issue_log_auto_creation_remains_disabled():
    batches = Path("app/routers/batches.py").read_text()
    # The legacy helper may remain for compatibility, but it must not be called
    # from the final batch processing path.
    tail = batches.split("# Issue Log is reserved for tenant-raised support tickets.", 1)[-1]
    assert "_create_issue_logs_for_review_rows" not in tail

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_invoice_file_model_has_durable_bytes_columns():
    src = read("app/db/models.py")
    assert "file_bytes" in src
    assert "storage_backend" in src
    assert "LargeBinary" in src


def test_upload_persists_pdf_bytes_to_database_copy():
    src = read("app/routers/batches.py")
    assert "attach_invoice_file_bytes(invoice_file, content)" in src
    assert "materialize_invoice_file(invoice_file)" in src


def test_review_preview_uses_materialized_pdf_path():
    src = read("app/routers/review.py")
    assert "materialize_invoice_file(f)" in src
    assert "PDF missing from local disk and no database file copy" in read("app/utils/persistent_files.py")


def test_startup_schema_adds_durable_file_columns():
    src = read("app/main.py")
    assert "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS file_bytes BYTEA" in src
    assert "ALTER TABLE invoice_files ADD COLUMN IF NOT EXISTS storage_backend" in src
    assert "ALTER TABLE batch_export_events ADD COLUMN IF NOT EXISTS file_bytes BYTEA" in src


def test_export_event_stores_generated_workbook_bytes():
    src = read("app/services/corrected_exporter.py")
    assert "export_bytes = out.getvalue()" in src
    assert "file_bytes=export_bytes" in src
    assert "storage_backend=\"database+local\"" in src

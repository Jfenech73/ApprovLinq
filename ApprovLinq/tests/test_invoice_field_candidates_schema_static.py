from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_invoice_field_candidate_model_exists_with_required_columns_and_indexes():
    src = read("app/db/review_models.py")
    assert "class InvoiceFieldCandidate(Base):" in src
    assert '__tablename__ = "invoice_field_candidates"' in src
    for token in [
        "tenant_id", "company_id", "batch_id", "row_id", "source_file_id",
        "field_name", "candidate_value", "normalised_value", "source_type", "source_id",
        "confidence", "evidence", "reason", "selected", "applied",
        "rejected_reason", "conflict", "created_at",
    ]:
        assert token in src
    for index_name in [
        "ix_field_candidates_tenant_company",
        "ix_field_candidates_batch_row",
        "ix_field_candidates_field_name",
        "ix_field_candidates_source_type",
        "ix_field_candidates_selected",
        "ix_field_candidates_created_at",
    ]:
        assert index_name in src


def test_alembic_schema_creates_invoice_field_candidates_table_and_indexes():
    src = read("alembic/versions/2026_05_13_0001_invoice_field_candidates.py")
    assert '"invoice_field_candidates"' in src
    assert '"tenant_id"' in src
    assert '"row_id"' in src
    assert '"confidence"' in src
    for index_name in [
        "ix_field_candidates_tenant_company",
        "ix_field_candidates_batch_row",
        "ix_field_candidates_field_name",
        "ix_field_candidates_source_type",
        "ix_field_candidates_selected",
        "ix_field_candidates_created_at",
    ]:
        assert index_name in src


def test_alembic_migration_exists_for_invoice_field_candidates():
    src = read("alembic/versions/2026_05_13_0001_invoice_field_candidates.py")
    assert 'revision = "20260513_0001"' in src
    assert 'down_revision = "20260411_0001"' in src
    assert '"invoice_field_candidates"' in src
    assert 'sa.Column("candidate_value", sa.Text()' in src
    assert 'sa.Column("normalised_value", sa.Text()' in src
    assert 'sa.Column("selected", sa.Boolean()' in src
    assert 'op.create_index(name, "invoice_field_candidates", cols)' in src


def test_candidate_persistence_is_enabled_after_later_candidate_phases():
    arbitration = read("app/services/invoice_arbitration.py")
    batches = read("app/routers/batches.py")
    combined = arbitration + batches
    assert "InvoiceFieldCandidate(" in combined
    assert "_persist_selected_field_candidates(" in batches

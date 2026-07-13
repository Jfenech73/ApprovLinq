from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.db import models
from app.db.schema_version import (
    CURRENT_ALEMBIC_REVISION,
    SchemaVersionError,
    assert_database_schema_current,
    validate_alembic_revision,
)


ROOT = Path(__file__).resolve().parents[1]


def test_schema_version_validator_accepts_only_current_head():
    validate_alembic_revision(CURRENT_ALEMBIC_REVISION)

    with pytest.raises(SchemaVersionError, match="behind build head"):
        validate_alembic_revision("20260710_0009")

    with pytest.raises(SchemaVersionError, match="not versioned"):
        validate_alembic_revision(None)

    with pytest.raises(SchemaVersionError, match="not recognised"):
        validate_alembic_revision("unknown_revision")


def test_sqlite_unit_fixture_engines_are_not_forced_through_alembic():
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    status = assert_database_schema_current(engine)
    assert status.checked is False
    assert status.dialect == "sqlite"


def test_production_startup_schema_mutation_has_been_removed():
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "assert_database_schema_current(engine)" in main_src
    assert "models.Base.metadata.create_all(bind=engine)" not in main_src
    assert "def ensure_runtime_schema" not in main_src
    assert "def ensure_alembic_head_marker" not in main_src
    assert "ALTER TABLE" not in main_src


def test_docker_runs_alembic_before_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "alembic upgrade head && uvicorn app.main:app" in dockerfile


def test_invoice_read_detail_provider_fields_are_prefixed_not_case_colliding():
    column_names = {column.name for column in models.InvoiceReadDetail.__table__.columns}
    assert {"description", "quantity", "unit_price"} <= column_names
    assert {"di_description", "di_quantity", "di_unit_price"} <= column_names
    assert {"Description", "Quantity", "UnitPrice"}.isdisjoint(column_names)

    lowered: dict[str, list[str]] = {}
    for name in column_names:
        lowered.setdefault(name.lower(), []).append(name)
    collisions = {key: names for key, names in lowered.items() if len(names) > 1}
    assert collisions == {}


def test_phase5_alembic_migration_declares_backfill():
    src = (ROOT / "alembic/versions/2026_07_10_0009_database_migration_foundation.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "20260710_0009"' in src
    assert 'down_revision = "20260710_0008"' in src
    for token in ["di_description", "di_quantity", "di_unit_price"]:
        assert token in src
    for token in ['"Description"', '"Quantity"', '"UnitPrice"']:
        assert token in src


def test_phase6_alembic_migration_declares_current_head_and_candidate_envelope():
    src = (ROOT / "alembic/versions/2026_07_10_0010_candidate_envelope_saved_regions_rules.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "20260710_0010"' in src
    assert 'down_revision = "20260710_0009"' in src
    for token in ["candidate_status", "validation_status", "region_id", "identity_score"]:
        assert token in src


def test_phase8_alembic_migration_declares_current_head_and_durable_jobs():
    src = (ROOT / "alembic/versions/2026_07_12_0011_durable_scan_jobs.py").read_text(
        encoding="utf-8"
    )
    assert f'revision = "{CURRENT_ALEMBIC_REVISION}"' in src
    assert 'down_revision = "20260710_0010"' in src
    for token in ["scan_jobs", "scan_job_pages", "lease_until", "heartbeat_at", "uq_scan_job_pages_run_file_page"]:
        assert token in src


@pytest.mark.integration
def test_postgres_clean_database_migrates_to_current_head():
    url = os.getenv("APPROVLINQ_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("APPROVLINQ_POSTGRES_TEST_URL not configured")
    if os.getenv("APPROVLINQ_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.skip("APPROVLINQ_ALLOW_DESTRUCTIVE_TEST_DB=1 is required for destructive migration tests")

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(url, future=True)
    try:
        status = assert_database_schema_current(engine)
        assert status.checked is True
        assert status.current_revision == CURRENT_ALEMBIC_REVISION
        with engine.connect() as conn:
            columns = {
                col["name"]
                for col in sa.inspect(conn).get_columns("invoice_read_details")
            }
        assert {"di_description", "di_quantity", "di_unit_price"} <= columns
        assert {"Description", "Quantity", "UnitPrice"}.isdisjoint(columns)
    finally:
        engine.dispose()

"""add immutable scan runs

Revision ID: 20260707_0006
Revises: 20260707_0005
Create Date: 2026-07-07
"""
from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260707_0006"
down_revision = "20260707_0005"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if table_name in _tables() and column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if table_name in _tables() and index_name not in _indexes(table_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    tables = _tables()
    if "scan_runs" not in tables:
        op.create_table(
            "scan_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("run_number", sa.Integer(), nullable=False),
            sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="processing"),
            sa.Column("app_version", sa.String(length=80), nullable=True),
            sa.Column("extractor_build_tag", sa.String(length=120), nullable=True),
            sa.Column("scan_mode", sa.String(length=20), nullable=True),
            sa.Column("settings_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("provider_config_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("selected_backend", sa.String(length=80), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    _create_index_if_missing("scan_runs", "ix_scan_runs_batch_number", ["batch_id", "run_number"])
    _create_index_if_missing("scan_runs", "ix_scan_runs_status", ["status"])
    _create_index_if_missing("scan_runs", "ix_scan_runs_parent", ["parent_run_id"])

    _add_column_if_missing("invoice_batches", sa.Column("current_scan_run_id", postgresql.UUID(as_uuid=True), nullable=True))
    for table_name in (
        "invoice_rows",
        "invoice_read_headers",
        "invoice_field_candidates",
        "invoice_row_corrections",
        "invoice_row_field_audits",
        "batch_export_events",
    ):
        _add_column_if_missing(
            table_name,
            sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
        )

    _create_index_if_missing("invoice_rows", "ix_invoice_rows_scan_run", ["scan_run_id"])
    _create_index_if_missing("invoice_read_headers", "ix_invoice_read_headers_scan_run", ["scan_run_id"])
    _create_index_if_missing("invoice_field_candidates", "ix_field_candidates_scan_run", ["scan_run_id"])
    _create_index_if_missing("invoice_row_corrections", "ix_corrections_scan_run", ["scan_run_id"])
    _create_index_if_missing("invoice_row_field_audits", "ix_audits_scan_run", ["scan_run_id"])
    _create_index_if_missing("batch_export_events", "ix_export_events_scan_run", ["scan_run_id"])

    bind = op.get_bind()
    if "invoice_batches" in _tables():
        batches = bind.execute(sa.text(
            "SELECT id, tenant_id, company_id, scan_mode, status, page_count, notes, created_at, processed_at "
            "FROM invoice_batches WHERE current_scan_run_id IS NULL"
        )).mappings().all()
        for batch in batches:
            run_id = str(uuid.uuid4())
            batch_id = str(batch["id"])
            row_count = None
            if "invoice_rows" in _tables():
                row_count = bind.execute(
                    sa.text("SELECT COUNT(*) FROM invoice_rows WHERE batch_id = :batch_id"),
                    {"batch_id": batch_id},
                ).scalar()
            bind.execute(sa.text(
                """
                INSERT INTO scan_runs (
                    id, batch_id, tenant_id, company_id, run_number, status,
                    scan_mode, page_count, row_count, notes, started_at, completed_at, created_at
                )
                VALUES (
                    :id, :batch_id, :tenant_id, :company_id, 1, :status,
                    :scan_mode, :page_count, :row_count, :notes,
                    COALESCE(:created_at, CURRENT_TIMESTAMP),
                    :processed_at,
                    COALESCE(:created_at, CURRENT_TIMESTAMP)
                )
                """
            ), {
                "id": run_id,
                "batch_id": batch_id,
                "tenant_id": str(batch["tenant_id"]) if batch["tenant_id"] else None,
                "company_id": str(batch["company_id"]) if batch["company_id"] else None,
                "status": str(batch["status"] or "backfilled"),
                "scan_mode": batch["scan_mode"],
                "page_count": batch["page_count"],
                "row_count": row_count,
                "notes": batch["notes"],
                "created_at": batch["created_at"],
                "processed_at": batch["processed_at"],
            })
            bind.execute(
                sa.text("UPDATE invoice_batches SET current_scan_run_id = :run_id WHERE id = :batch_id"),
                {"run_id": run_id, "batch_id": batch_id},
            )
            for table_name in (
                "invoice_rows",
                "invoice_read_headers",
                "invoice_field_candidates",
                "invoice_row_corrections",
                "invoice_row_field_audits",
                "batch_export_events",
            ):
                if table_name in _tables() and "scan_run_id" in _columns(table_name):
                    bind.execute(
                        sa.text(f"UPDATE {table_name} SET scan_run_id = :run_id WHERE batch_id = :batch_id AND scan_run_id IS NULL"),
                        {"run_id": run_id, "batch_id": batch_id},
                    )


def downgrade() -> None:
    for table_name, index_name in (
        ("batch_export_events", "ix_export_events_scan_run"),
        ("invoice_row_field_audits", "ix_audits_scan_run"),
        ("invoice_row_corrections", "ix_corrections_scan_run"),
        ("invoice_field_candidates", "ix_field_candidates_scan_run"),
        ("invoice_read_headers", "ix_invoice_read_headers_scan_run"),
        ("invoice_rows", "ix_invoice_rows_scan_run"),
        ("scan_runs", "ix_scan_runs_parent"),
        ("scan_runs", "ix_scan_runs_status"),
        ("scan_runs", "ix_scan_runs_batch_number"),
    ):
        if table_name in _tables() and index_name in _indexes(table_name):
            op.drop_index(index_name, table_name=table_name)

    for table_name in (
        "batch_export_events",
        "invoice_row_field_audits",
        "invoice_row_corrections",
        "invoice_field_candidates",
        "invoice_read_headers",
        "invoice_rows",
    ):
        if table_name in _tables() and "scan_run_id" in _columns(table_name):
            op.drop_column(table_name, "scan_run_id")

    if "invoice_batches" in _tables() and "current_scan_run_id" in _columns("invoice_batches"):
        op.drop_column("invoice_batches", "current_scan_run_id")
    if "scan_runs" in _tables():
        op.drop_table("scan_runs")

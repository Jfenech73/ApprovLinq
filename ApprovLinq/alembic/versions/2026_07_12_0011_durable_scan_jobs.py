"""add durable scan job queue

Revision ID: 20260712_0011
Revises: 20260710_0010
Create Date: 2026-07-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260712_0011"
down_revision = "20260710_0010"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _constraints(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_unique_constraints(table_name) if c.get("name")}


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if table_name in _tables() and index_name not in _indexes(table_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    tables = _tables()
    if "scan_jobs" not in tables:
        op.create_table(
            "scan_jobs",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="queued"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("lease_owner", sa.String(length=120), nullable=True),
            sa.Column("lease_token", sa.String(length=80), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    if "scan_job_pages" not in tables:
        op.create_table(
            "scan_job_pages",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_file_id", sa.BigInteger(), sa.ForeignKey("invoice_files.id", ondelete="CASCADE"), nullable=False),
            sa.Column("page_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="queued"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("lease_owner", sa.String(length=120), nullable=True),
            sa.Column("lease_token", sa.String(length=80), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("row_count", sa.Integer(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("scan_run_id", "source_file_id", "page_no", name="uq_scan_job_pages_run_file_page"),
        )

    for table_name, index_name, columns in (
        ("scan_jobs", "ix_scan_jobs_status", ["status"]),
        ("scan_jobs", "ix_scan_jobs_lease_until", ["lease_until"]),
        ("scan_jobs", "ix_scan_jobs_batch", ["batch_id"]),
        ("scan_jobs", "ix_scan_jobs_scan_run", ["scan_run_id"]),
        ("scan_jobs", "ix_scan_jobs_created_at", ["created_at"]),
        ("scan_job_pages", "ix_scan_job_pages_job", ["job_id"]),
        ("scan_job_pages", "ix_scan_job_pages_status", ["status"]),
        ("scan_job_pages", "ix_scan_job_pages_lease_until", ["lease_until"]),
        ("scan_job_pages", "ix_scan_job_pages_batch", ["batch_id"]),
        ("scan_job_pages", "ix_scan_job_pages_scan_run", ["scan_run_id"]),
    ):
        _create_index_if_missing(table_name, index_name, columns)


def downgrade() -> None:
    for table_name, index_name in (
        ("scan_job_pages", "ix_scan_job_pages_scan_run"),
        ("scan_job_pages", "ix_scan_job_pages_batch"),
        ("scan_job_pages", "ix_scan_job_pages_lease_until"),
        ("scan_job_pages", "ix_scan_job_pages_status"),
        ("scan_job_pages", "ix_scan_job_pages_job"),
        ("scan_jobs", "ix_scan_jobs_created_at"),
        ("scan_jobs", "ix_scan_jobs_scan_run"),
        ("scan_jobs", "ix_scan_jobs_batch"),
        ("scan_jobs", "ix_scan_jobs_lease_until"),
        ("scan_jobs", "ix_scan_jobs_status"),
    ):
        if table_name in _tables() and index_name in _indexes(table_name):
            op.drop_index(index_name, table_name=table_name)
    if "scan_job_pages" in _tables():
        op.drop_table("scan_job_pages")
    if "scan_jobs" in _tables():
        op.drop_table("scan_jobs")

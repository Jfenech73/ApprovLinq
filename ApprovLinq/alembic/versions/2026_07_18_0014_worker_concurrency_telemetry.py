"""add worker concurrency and provider telemetry fields

Revision ID: 20260718_0014
Revises: 20260713_0013
Create Date: 2026-07-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260718_0014"
down_revision = "20260713_0013"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    table = "scan_job_pages"
    for column in (
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_name", sa.String(length=80), nullable=True),
        sa.Column("provider_status", sa.String(length=80), nullable=True),
        sa.Column("provider_operation_id", sa.String(length=160), nullable=True),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("provider_latency_ms", sa.Integer(), nullable=True),
        sa.Column("provider_retries", sa.Integer(), nullable=True),
        sa.Column("provider_timeout_reason", sa.String(length=255), nullable=True),
        sa.Column("provider_telemetry", sa.JSON(), nullable=True),
    ):
        _add_column_if_missing(table, column)


def downgrade() -> None:
    table = "scan_job_pages"
    for name in (
        "provider_telemetry",
        "provider_timeout_reason",
        "provider_retries",
        "provider_latency_ms",
        "provider_request_id",
        "provider_operation_id",
        "provider_status",
        "provider_name",
        "cancel_requested_at",
    ):
        if name in _columns(table):
            op.drop_column(table, name)

"""add invoice row soft-block status

Revision ID: 20260710_0007
Revises: 20260707_0006
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260710_0007"
down_revision = "20260707_0006"
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


def upgrade() -> None:
    if "invoice_rows" not in _tables():
        return

    _add_column_if_missing(
        "invoice_rows",
        sa.Column("row_status", sa.String(length=40), nullable=False, server_default="active"),
    )
    _add_column_if_missing("invoice_rows", sa.Column("row_status_reason", sa.String(length=80), nullable=True))
    _add_column_if_missing("invoice_rows", sa.Column("row_status_note", sa.Text(), nullable=True))
    _add_column_if_missing("invoice_rows", sa.Column("row_status_changed_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing(
        "invoice_rows",
        sa.Column(
            "row_status_changed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.execute("UPDATE invoice_rows SET row_status = 'active' WHERE row_status IS NULL OR row_status = ''")
    if "ix_invoice_rows_export_status" not in _indexes("invoice_rows"):
        op.create_index(
            "ix_invoice_rows_export_status",
            "invoice_rows",
            ["batch_id", "scan_run_id", "row_status"],
        )


def downgrade() -> None:
    if "invoice_rows" not in _tables():
        return
    if "ix_invoice_rows_export_status" in _indexes("invoice_rows"):
        op.drop_index("ix_invoice_rows_export_status", table_name="invoice_rows")
    for column_name in (
        "row_status_changed_by",
        "row_status_changed_at",
        "row_status_note",
        "row_status_reason",
        "row_status",
    ):
        if column_name in _columns("invoice_rows"):
            op.drop_column("invoice_rows", column_name)

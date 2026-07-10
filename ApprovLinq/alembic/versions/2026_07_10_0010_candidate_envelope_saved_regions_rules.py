"""add candidate envelope fields for saved regions and rules

Revision ID: 20260710_0010
Revises: 20260710_0009
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260710_0010"
down_revision = "20260710_0009"
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
    if column.name not in _columns(table_name):
        op.add_column(table_name, column.copy())


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    if column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if index_name not in _indexes(table_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    if "invoice_field_candidates" not in _tables():
        return

    for column in (
        sa.Column("candidate_status", sa.String(length=40), nullable=False, server_default="candidate"),
        sa.Column("validation_status", sa.String(length=40), nullable=True),
        sa.Column("validation_reason", sa.Text(), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("region_id", sa.BigInteger(), nullable=True),
        sa.Column("identity_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("evidence_ref_type", sa.String(length=80), nullable=True),
        sa.Column("evidence_ref_id", sa.Text(), nullable=True),
    ):
        _add_column_if_missing("invoice_field_candidates", column)

    op.execute(
        "UPDATE invoice_field_candidates SET candidate_status = 'candidate' "
        "WHERE candidate_status IS NULL OR candidate_status = ''"
    )
    _create_index_if_missing("invoice_field_candidates", "ix_field_candidates_status", ["candidate_status"])
    _create_index_if_missing("invoice_field_candidates", "ix_field_candidates_region", ["region_id"])


def downgrade() -> None:
    if "invoice_field_candidates" not in _tables():
        return
    for index_name in ("ix_field_candidates_region", "ix_field_candidates_status"):
        if index_name in _indexes("invoice_field_candidates"):
            op.drop_index(index_name, table_name="invoice_field_candidates")
    for column_name in (
        "evidence_ref_id",
        "evidence_ref_type",
        "identity_score",
        "region_id",
        "page_no",
        "validation_reason",
        "validation_status",
        "candidate_status",
    ):
        _drop_column_if_present("invoice_field_candidates", column_name)

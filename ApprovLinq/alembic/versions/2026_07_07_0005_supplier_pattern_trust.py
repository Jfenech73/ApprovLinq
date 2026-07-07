"""add trust lifecycle to supplier patterns

Revision ID: 20260707_0005
Revises: 20260516_0004
Create Date: 2026-07-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260707_0005"
down_revision = "20260516_0004"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    return table_name in set(sa.inspect(bind).get_table_names())


def _ensure_columns(table_name: str, columns: list[sa.Column]) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {col["name"] for col in insp.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _ensure_index(table_name: str, index_name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = {idx["name"] for idx in insp.get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    if not _has_table("supplier_patterns"):
        return

    _ensure_columns("supplier_patterns", [
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("trusted_outcome_source", sa.String(length=40), nullable=True),
        sa.Column("source_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_row_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_trusted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposed_keywords", sa.Text(), nullable=True),
        sa.Column("proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_proposed_at", sa.DateTime(timezone=True), nullable=True),
    ])
    op.execute("UPDATE supplier_patterns SET status = 'active' WHERE status IS NULL OR status = ''")
    _ensure_index("supplier_patterns", "ix_supplier_patterns_tenant_company_status", ["tenant_id", "company_id", "status"])
    _ensure_index("supplier_patterns", "ix_supplier_patterns_supplier_status", ["tenant_id", "company_id", "supplier_id", "status"])


def downgrade() -> None:
    if not _has_table("supplier_patterns"):
        return

    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes("supplier_patterns")}
    for index_name in (
        "ix_supplier_patterns_supplier_status",
        "ix_supplier_patterns_tenant_company_status",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="supplier_patterns")

    existing_cols = {col["name"] for col in insp.get_columns("supplier_patterns")}
    for column_name in (
        "last_proposed_at",
        "proposal_count",
        "proposed_keywords",
        "last_trusted_at",
        "activated_by",
        "activated_at",
        "created_by",
        "source_row_id",
        "source_batch_id",
        "trusted_outcome_source",
        "status",
    ):
        if column_name in existing_cols:
            op.drop_column("supplier_patterns", column_name)

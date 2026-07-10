"""add cross-batch duplicate evidence

Revision ID: 20260710_0008
Revises: 20260710_0007
Create Date: 2026-07-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260710_0008"
down_revision = "20260710_0007"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table_name: str) -> set[str]:
    if table_name not in _tables():
        return set()
    return {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    if "invoice_duplicate_candidates" not in _tables():
        op.create_table(
            "invoice_duplicate_candidates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("candidate_batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("candidate_scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("candidate_row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("match_type", sa.String(length=40), nullable=False, server_default="cross_batch"),
            sa.Column("match_status", sa.String(length=40), nullable=False),
            sa.Column("confidence", sa.Numeric(6, 4), nullable=True),
            sa.Column("evidence_json", sa.Text(), nullable=True),
            sa.Column("normalized_invoice_number", sa.String(length=160), nullable=True),
            sa.Column("document_type", sa.String(length=80), nullable=True),
            sa.Column("supplier_key", sa.String(length=255), nullable=True),
            sa.Column("supplier_vat", sa.String(length=100), nullable=True),
            sa.Column("invoice_date", sa.Date(), nullable=True),
            sa.Column("total_cents", sa.BigInteger(), nullable=True),
            sa.Column("currency", sa.String(length=20), nullable=True),
            sa.Column("document_fingerprint", sa.String(length=80), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        )

    for name, cols, unique in (
        ("ix_duplicate_candidates_tenant_company", ["tenant_id", "company_id"], False),
        ("ix_duplicate_candidates_batch_row", ["batch_id", "row_id"], False),
        ("ix_duplicate_candidates_candidate_row", ["candidate_batch_id", "candidate_row_id"], False),
        ("ix_duplicate_candidates_scan_run", ["scan_run_id"], False),
        ("ix_duplicate_candidates_status", ["match_status"], False),
        ("uq_duplicate_candidates_pair_type", ["row_id", "candidate_row_id", "match_type"], True),
    ):
        if name not in _indexes("invoice_duplicate_candidates"):
            op.create_index(name, "invoice_duplicate_candidates", cols, unique=unique)


def downgrade() -> None:
    if "invoice_duplicate_candidates" not in _tables():
        return
    for name in (
        "uq_duplicate_candidates_pair_type",
        "ix_duplicate_candidates_status",
        "ix_duplicate_candidates_scan_run",
        "ix_duplicate_candidates_candidate_row",
        "ix_duplicate_candidates_batch_row",
        "ix_duplicate_candidates_tenant_company",
    ):
        if name in _indexes("invoice_duplicate_candidates"):
            op.drop_index(name, table_name="invoice_duplicate_candidates")
    op.drop_table("invoice_duplicate_candidates")

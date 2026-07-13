"""add approved invoice facts for expense insights

Revision ID: 20260713_0013
Revises: 20260713_0012
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260713_0013"
down_revision = "20260713_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approved_invoice_facts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_row_id", sa.BigInteger(), sa.ForeignKey("invoice_rows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("export_event_id", sa.BigInteger(), sa.ForeignKey("batch_export_events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("export_version", sa.Integer(), nullable=False),
        sa.Column("fact_version", sa.Integer(), nullable=False),
        sa.Column("fact_fingerprint", sa.String(80), nullable=False),
        sa.Column("evidence_ref_type", sa.String(80), nullable=False),
        sa.Column("evidence_ref_id", sa.Text(), nullable=False),
        sa.Column("canonical_supplier_name", sa.Text(), nullable=True),
        sa.Column("supplier_posting_account", sa.String(100), nullable=True),
        sa.Column("supplier_vat", sa.String(100), nullable=True),
        sa.Column("document_type", sa.String(80), nullable=True),
        sa.Column("invoice_number", sa.Text(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("nominal_account_code", sa.String(100), nullable=True),
        sa.Column("nominal_account_name", sa.String(255), nullable=True),
        sa.Column("category", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(20), nullable=True),
        sa.Column("reporting_currency", sa.String(20), nullable=True),
        sa.Column("tax_code", sa.String(50), nullable=True),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("vat_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("reporting_net_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("reporting_vat_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("reporting_total_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("bcrs_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("deposit_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("source_row_status", sa.String(40), nullable=True),
        sa.Column("source_validation_status", sa.String(100), nullable=True),
        sa.Column("source_review_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("duplicate_exposure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_exposure_status", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("batch_id", "source_row_id", "fact_version", name="uq_approved_fact_batch_row_version"),
    )
    op.create_index("ix_approved_facts_tenant_company_date", "approved_invoice_facts", ["tenant_id", "company_id", "invoice_date"])
    op.create_index("ix_approved_facts_supplier", "approved_invoice_facts", ["tenant_id", "company_id", "canonical_supplier_name"])
    op.create_index("ix_approved_facts_nominal", "approved_invoice_facts", ["tenant_id", "company_id", "nominal_account_code"])
    op.create_index("ix_approved_facts_batch_version", "approved_invoice_facts", ["batch_id", "fact_version"])
    op.create_index("ix_approved_facts_source_row", "approved_invoice_facts", ["source_row_id"])
    op.create_index("ix_approved_facts_export_event", "approved_invoice_facts", ["export_event_id"])


def downgrade() -> None:
    op.drop_index("ix_approved_facts_export_event", table_name="approved_invoice_facts")
    op.drop_index("ix_approved_facts_source_row", table_name="approved_invoice_facts")
    op.drop_index("ix_approved_facts_batch_version", table_name="approved_invoice_facts")
    op.drop_index("ix_approved_facts_nominal", table_name="approved_invoice_facts")
    op.drop_index("ix_approved_facts_supplier", table_name="approved_invoice_facts")
    op.drop_index("ix_approved_facts_tenant_company_date", table_name="approved_invoice_facts")
    op.drop_table("approved_invoice_facts")

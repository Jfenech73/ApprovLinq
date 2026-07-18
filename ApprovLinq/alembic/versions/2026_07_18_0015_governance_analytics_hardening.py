"""governance and analytics hardening

Revision ID: 20260718_0015
Revises: 20260718_0014
Create Date: 2026-07-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260718_0015"
down_revision = "20260718_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learning_recommendation_proposals",
        sa.Column("canary_required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "learning_recommendation_proposals",
        sa.Column("canary_status", sa.String(40), nullable=False, server_default="pending"),
    )
    op.add_column(
        "learning_recommendation_proposals",
        sa.Column("canary_passed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "learning_recommendation_proposals",
        sa.Column("rollback_required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "learning_recommendation_proposals",
        sa.Column("governance_reason", sa.Text(), nullable=True),
    )
    op.add_column("learning_promotions", sa.Column("canary_snapshot_json", sa.JSON(), nullable=True))
    op.add_column("learning_promotions", sa.Column("rollback_verified_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("approved_invoice_facts", sa.Column("deposit_component_type", sa.String(40), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("deposit_component_source", sa.String(120), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("deposit_component_provenance_json", sa.JSON(), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("fx_rate_source", sa.String(120), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("fx_rate_date", sa.Date(), nullable=True))
    op.add_column("approved_invoice_facts", sa.Column("fx_rate_provenance_json", sa.JSON(), nullable=True))

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("currency", sa.String(20), nullable=False),
        sa.Column("reporting_currency", sa.String(20), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(120), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id",
            "currency",
            "reporting_currency",
            "rate_date",
            "source",
            name="uq_fx_rates_scope_date_source",
        ),
    )
    op.create_index(
        "ix_fx_rates_tenant_pair_date",
        "fx_rates",
        ["tenant_id", "currency", "reporting_currency", "rate_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_fx_rates_tenant_pair_date", table_name="fx_rates")
    op.drop_table("fx_rates")

    for column in [
        "fx_rate_provenance_json",
        "fx_rate_date",
        "fx_rate_source",
        "fx_rate",
        "deposit_component_provenance_json",
        "deposit_component_source",
        "deposit_component_type",
    ]:
        op.drop_column("approved_invoice_facts", column)

    op.drop_column("learning_promotions", "rollback_verified_at")
    op.drop_column("learning_promotions", "canary_snapshot_json")
    for column in [
        "governance_reason",
        "rollback_required",
        "canary_passed_at",
        "canary_status",
        "canary_required",
    ]:
        op.drop_column("learning_recommendation_proposals", column)

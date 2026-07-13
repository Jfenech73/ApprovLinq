"""add controlled learning recommendation agent tables

Revision ID: 20260713_0012
Revises: 20260712_0011
Create Date: 2026-07-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260713_0012"
down_revision = "20260712_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_recommendation_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="running"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("proposals_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_learning_runs_tenant_company", "learning_recommendation_runs", ["tenant_id", "company_id"])
    op.create_index("ix_learning_runs_status", "learning_recommendation_runs", ["status"])
    op.create_index("ix_learning_runs_created_at", "learning_recommendation_runs", ["created_at"])

    op.create_table(
        "learning_recommendation_proposals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("learning_recommendation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("proposal_type", sa.String(80), nullable=False),
        sa.Column("target_entity_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="proposed"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("proposed_payload_json", sa.JSON(), nullable=False),
        sa.Column("canary_scope_json", sa.JSON(), nullable=True),
        sa.Column("evidence_summary_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Numeric(6, 4), nullable=True),
        sa.Column("quality_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("latency_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("cost_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("risk_level", sa.String(40), nullable=False, server_default="medium"),
        sa.Column("fingerprint", sa.String(80), nullable=False),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learning_proposals_run", "learning_recommendation_proposals", ["run_id"])
    op.create_index("ix_learning_proposals_tenant_company", "learning_recommendation_proposals", ["tenant_id", "company_id"])
    op.create_index("ix_learning_proposals_status", "learning_recommendation_proposals", ["status"])
    op.create_index("ix_learning_proposals_type", "learning_recommendation_proposals", ["proposal_type"])
    op.create_index("ix_learning_proposals_fingerprint", "learning_recommendation_proposals", ["fingerprint"])

    op.create_table(
        "learning_recommendation_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), sa.ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_type", sa.String(80), nullable=False),
        sa.Column("source_table", sa.String(120), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("row_id", sa.BigInteger(), nullable=True),
        sa.Column("field_name", sa.String(80), nullable=True),
        sa.Column("observed_value", sa.Text(), nullable=True),
        sa.Column("expected_value", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learning_evidence_proposal", "learning_recommendation_evidence", ["proposal_id"])
    op.create_index("ix_learning_evidence_batch_row", "learning_recommendation_evidence", ["batch_id", "row_id"])

    op.create_table(
        "learning_recommendation_replay_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), sa.ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoice_batches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("scan_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("row_id", sa.BigInteger(), nullable=True),
        sa.Column("replay_status", sa.String(40), nullable=False),
        sa.Column("changed_fields_json", sa.JSON(), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("quality_score", sa.Numeric(6, 4), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_estimate", sa.Numeric(10, 4), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learning_replay_proposal", "learning_recommendation_replay_results", ["proposal_id"])
    op.create_index("ix_learning_replay_batch_row", "learning_recommendation_replay_results", ["batch_id", "row_id"])

    op.create_table(
        "learning_reviewer_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), sa.ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learning_decisions_proposal", "learning_reviewer_decisions", ["proposal_id"])

    op.create_table(
        "learning_promotions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), sa.ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("promoted_entity_type", sa.String(80), nullable=False),
        sa.Column("promoted_entity_id", sa.Text(), nullable=False),
        sa.Column("previous_state_json", sa.JSON(), nullable=True),
        sa.Column("promoted_state_json", sa.JSON(), nullable=True),
        sa.Column("rollback_state_json", sa.JSON(), nullable=True),
        sa.Column("promoted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("rollback_status", sa.String(40), nullable=True),
        sa.Column("rolled_back_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_learning_promotions_proposal", "learning_promotions", ["proposal_id"])
    op.create_index("ix_learning_promotions_entity", "learning_promotions", ["promoted_entity_type", "promoted_entity_id"])


def downgrade() -> None:
    op.drop_index("ix_learning_promotions_entity", table_name="learning_promotions")
    op.drop_index("ix_learning_promotions_proposal", table_name="learning_promotions")
    op.drop_table("learning_promotions")
    op.drop_index("ix_learning_decisions_proposal", table_name="learning_reviewer_decisions")
    op.drop_table("learning_reviewer_decisions")
    op.drop_index("ix_learning_replay_batch_row", table_name="learning_recommendation_replay_results")
    op.drop_index("ix_learning_replay_proposal", table_name="learning_recommendation_replay_results")
    op.drop_table("learning_recommendation_replay_results")
    op.drop_index("ix_learning_evidence_batch_row", table_name="learning_recommendation_evidence")
    op.drop_index("ix_learning_evidence_proposal", table_name="learning_recommendation_evidence")
    op.drop_table("learning_recommendation_evidence")
    op.drop_index("ix_learning_proposals_fingerprint", table_name="learning_recommendation_proposals")
    op.drop_index("ix_learning_proposals_type", table_name="learning_recommendation_proposals")
    op.drop_index("ix_learning_proposals_status", table_name="learning_recommendation_proposals")
    op.drop_index("ix_learning_proposals_tenant_company", table_name="learning_recommendation_proposals")
    op.drop_index("ix_learning_proposals_run", table_name="learning_recommendation_proposals")
    op.drop_table("learning_recommendation_proposals")
    op.drop_index("ix_learning_runs_created_at", table_name="learning_recommendation_runs")
    op.drop_index("ix_learning_runs_status", table_name="learning_recommendation_runs")
    op.drop_index("ix_learning_runs_tenant_company", table_name="learning_recommendation_runs")
    op.drop_table("learning_recommendation_runs")

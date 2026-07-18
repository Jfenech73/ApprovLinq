from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base
from app.db.review_models import _BIGINT_PK, _utcnow


LEARNING_RUN_STATUS_RUNNING = "running"
LEARNING_RUN_STATUS_COMPLETED = "completed"
LEARNING_RUN_STATUS_FAILED = "failed"

LEARNING_PROPOSAL_STATUS_PROPOSED = "proposed"
LEARNING_PROPOSAL_STATUS_APPROVED = "approved"
LEARNING_PROPOSAL_STATUS_REJECTED = "rejected"
LEARNING_PROPOSAL_STATUS_PROMOTED = "promoted"
LEARNING_PROPOSAL_STATUS_ROLLED_BACK = "rolled_back"

LEARNING_DECISION_APPROVED = "approved"
LEARNING_DECISION_REJECTED = "rejected"

LEARNING_CANARY_STATUS_PENDING = "pending"
LEARNING_CANARY_STATUS_PASSED = "passed"
LEARNING_CANARY_STATUS_FAILED = "failed"


class LearningRecommendationRun(Base):
    __tablename__ = "learning_recommendation_runs"
    __table_args__ = (
        Index("ix_learning_runs_tenant_company", "tenant_id", "company_id"),
        Index("ix_learning_runs_status", "status"),
        Index("ix_learning_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default=LEARNING_RUN_STATUS_RUNNING, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposals_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replay_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LearningRecommendationProposal(Base):
    __tablename__ = "learning_recommendation_proposals"
    __table_args__ = (
        Index("ix_learning_proposals_run", "run_id"),
        Index("ix_learning_proposals_tenant_company", "tenant_id", "company_id"),
        Index("ix_learning_proposals_status", "status"),
        Index("ix_learning_proposals_type", "proposal_type"),
        Index("ix_learning_proposals_fingerprint", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("learning_recommendation_runs.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    proposal_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=LEARNING_PROPOSAL_STATUS_PROPOSED, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    canary_scope_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    canary_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    canary_status: Mapped[str] = mapped_column(String(40), default=LEARNING_CANARY_STATUS_PENDING, nullable=False)
    canary_passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    governance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_summary_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    latency_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    cost_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(40), default="medium", nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class LearningRecommendationEvidence(Base):
    __tablename__ = "learning_recommendation_evidence"
    __table_args__ = (
        Index("ix_learning_evidence_proposal", "proposal_id"),
        Index("ix_learning_evidence_batch_row", "batch_id", "row_id"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_table: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoice_batches.id", ondelete="SET NULL"), nullable=True)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    row_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    field_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    observed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class LearningRecommendationReplayResult(Base):
    __tablename__ = "learning_recommendation_replay_results"
    __table_args__ = (
        Index("ix_learning_replay_proposal", "proposal_id"),
        Index("ix_learning_replay_batch_row", "batch_id", "row_id"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoice_batches.id", ondelete="SET NULL"), nullable=True)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True)
    row_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replay_status: Mapped[str] = mapped_column(String(40), nullable=False)
    changed_fields_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_estimate: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class LearningReviewerDecision(Base):
    __tablename__ = "learning_reviewer_decisions"
    __table_args__ = (
        Index("ix_learning_decisions_proposal", "proposal_id"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class LearningPromotion(Base):
    __tablename__ = "learning_promotions"
    __table_args__ = (
        Index("ix_learning_promotions_proposal", "proposal_id"),
        Index("ix_learning_promotions_entity", "promoted_entity_type", "promoted_entity_id"),
    )

    id: Mapped[int] = mapped_column(_BIGINT_PK, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("learning_recommendation_proposals.id", ondelete="CASCADE"), nullable=False)
    promoted_entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    promoted_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    previous_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    promoted_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rollback_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    canary_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    promoted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    rollback_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rollback_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

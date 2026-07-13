from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.learning_models import (
    LearningPromotion,
    LearningRecommendationEvidence,
    LearningRecommendationProposal,
    LearningRecommendationReplayResult,
)
from app.db.session import get_db
from app.routers.auth import current_tenant_id, current_user
from app.services.learning_recommendation_agent import (
    approve_proposal,
    promote_proposal,
    reject_proposal,
    rollback_promotion,
    run_learning_recommendation_agent,
)


router = APIRouter(prefix="/learning", tags=["learning"])


class LearningRunIn(BaseModel):
    company_id: UUID | None = None


class DecisionIn(BaseModel):
    note: str | None = None


def _serialise_proposal(p: LearningRecommendationProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "run_id": p.run_id,
        "tenant_id": str(p.tenant_id),
        "company_id": str(p.company_id) if p.company_id else None,
        "proposal_type": p.proposal_type,
        "target_entity_type": p.target_entity_type,
        "status": p.status,
        "title": p.title,
        "summary": p.summary,
        "proposed_payload": p.proposed_payload_json,
        "canary_scope": p.canary_scope_json,
        "evidence_summary": p.evidence_summary_json,
        "confidence": float(p.confidence) if p.confidence is not None else None,
        "quality_score": float(p.quality_score) if p.quality_score is not None else None,
        "risk_level": p.risk_level,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _proposal_for_tenant(db: Session, proposal_id: int, tenant_id) -> LearningRecommendationProposal:
    proposal = db.get(LearningRecommendationProposal, proposal_id)
    if proposal is None or proposal.tenant_id != tenant_id:
        raise HTTPException(404, "Learning proposal not found")
    return proposal


@router.post("/runs")
def create_learning_run(
    payload: LearningRunIn,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user=Depends(current_user),
):
    run = run_learning_recommendation_agent(
        db,
        tenant_id=tenant_id,
        company_id=payload.company_id,
        requested_by=getattr(user, "id", None),
    )
    db.commit()
    return {
        "id": run.id,
        "status": run.status,
        "tenant_id": str(run.tenant_id),
        "company_id": str(run.company_id) if run.company_id else None,
        "summary": run.summary_json,
    }


@router.get("/proposals")
def list_proposals(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user=Depends(current_user),
):
    q = select(LearningRecommendationProposal).where(LearningRecommendationProposal.tenant_id == tenant_id)
    if status:
        q = q.where(LearningRecommendationProposal.status == status)
    proposals = db.execute(q.order_by(LearningRecommendationProposal.created_at.desc())).scalars().all()
    return [_serialise_proposal(p) for p in proposals]


@router.get("/proposals/{proposal_id}")
def get_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    _user=Depends(current_user),
):
    proposal = _proposal_for_tenant(db, proposal_id, tenant_id)
    evidence = db.execute(
        select(LearningRecommendationEvidence).where(LearningRecommendationEvidence.proposal_id == proposal.id)
    ).scalars().all()
    replay = db.execute(
        select(LearningRecommendationReplayResult).where(LearningRecommendationReplayResult.proposal_id == proposal.id)
    ).scalars().all()
    data = _serialise_proposal(proposal)
    data["evidence"] = [
        {
            "id": e.id,
            "evidence_type": e.evidence_type,
            "source_table": e.source_table,
            "source_id": e.source_id,
            "batch_id": str(e.batch_id) if e.batch_id else None,
            "scan_run_id": str(e.scan_run_id) if e.scan_run_id else None,
            "row_id": e.row_id,
            "field_name": e.field_name,
            "observed_value": e.observed_value,
            "expected_value": e.expected_value,
            "evidence": e.evidence_json,
        }
        for e in evidence
    ]
    data["replay_results"] = [
        {
            "id": r.id,
            "batch_id": str(r.batch_id) if r.batch_id else None,
            "scan_run_id": str(r.scan_run_id) if r.scan_run_id else None,
            "row_id": r.row_id,
            "replay_status": r.replay_status,
            "changed_fields": r.changed_fields_json,
            "before": r.before_json,
            "after": r.after_json,
            "quality_score": float(r.quality_score) if r.quality_score is not None else None,
            "latency_ms": r.latency_ms,
            "cost_estimate": float(r.cost_estimate) if r.cost_estimate is not None else None,
        }
        for r in replay
    ]
    return data


@router.post("/proposals/{proposal_id}/approve")
def approve_learning_proposal(
    proposal_id: int,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user=Depends(current_user),
):
    _proposal_for_tenant(db, proposal_id, tenant_id)
    try:
        proposal = approve_proposal(db, proposal_id, user=user, note=payload.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return _serialise_proposal(proposal)


@router.post("/proposals/{proposal_id}/reject")
def reject_learning_proposal(
    proposal_id: int,
    payload: DecisionIn,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user=Depends(current_user),
):
    _proposal_for_tenant(db, proposal_id, tenant_id)
    try:
        proposal = reject_proposal(db, proposal_id, user=user, note=payload.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return _serialise_proposal(proposal)


@router.post("/proposals/{proposal_id}/promote")
def promote_learning_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user=Depends(current_user),
):
    _proposal_for_tenant(db, proposal_id, tenant_id)
    try:
        promotion = promote_proposal(db, proposal_id, user=user)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return {
        "id": promotion.id,
        "proposal_id": promotion.proposal_id,
        "promoted_entity_type": promotion.promoted_entity_type,
        "promoted_entity_id": promotion.promoted_entity_id,
        "rollback_status": promotion.rollback_status,
    }


@router.post("/promotions/{promotion_id}/rollback")
def rollback_learning_promotion(
    promotion_id: int,
    db: Session = Depends(get_db),
    tenant_id=Depends(current_tenant_id),
    user=Depends(current_user),
):
    promotion = db.get(LearningPromotion, promotion_id)
    proposal = db.get(LearningRecommendationProposal, promotion.proposal_id) if promotion else None
    if promotion is None or proposal is None or proposal.tenant_id != tenant_id:
        raise HTTPException(404, "Learning promotion not found")
    try:
        promotion = rollback_promotion(db, promotion_id, user=user)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return {
        "id": promotion.id,
        "proposal_id": promotion.proposal_id,
        "rollback_status": promotion.rollback_status,
    }

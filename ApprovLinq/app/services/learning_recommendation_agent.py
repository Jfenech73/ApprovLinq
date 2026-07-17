from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.learning_models import (
    LEARNING_DECISION_APPROVED,
    LEARNING_DECISION_REJECTED,
    LEARNING_PROPOSAL_STATUS_APPROVED,
    LEARNING_PROPOSAL_STATUS_PROPOSED,
    LEARNING_PROPOSAL_STATUS_PROMOTED,
    LEARNING_PROPOSAL_STATUS_REJECTED,
    LEARNING_PROPOSAL_STATUS_ROLLED_BACK,
    LEARNING_RUN_STATUS_COMPLETED,
    LEARNING_RUN_STATUS_FAILED,
    LEARNING_RUN_STATUS_RUNNING,
    LearningPromotion,
    LearningRecommendationEvidence,
    LearningRecommendationProposal,
    LearningRecommendationReplayResult,
    LearningRecommendationRun,
    LearningReviewerDecision,
)
from app.db.review_models import CorrectionRule, InvoiceFieldCandidate, RemapHint
from app.services.candidate_outcomes import normalise_outcome_value
from app.services.export_eligibility import DEFAULT_EXPORT_ELIGIBILITY_POLICY


RECOMMENDABLE_RULE_FIELDS = frozenset({"supplier_name", "nominal_account_code"})
TRUSTED_OUTCOME_SOURCES = frozenset({"manual_review", "mark_reviewed", "approved", "export"})


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rule_type_for_field(field_name: str) -> str:
    return "supplier_alias" if field_name == "supplier_name" else "nominal_remap"


def _proposal_title(field_name: str, source: str, target: str) -> str:
    label = field_name.replace("_", " ")
    return f"Recommend {label} rule from repeated accepted corrections"


def _active_rule_exists(db: Session, *, tenant_id: Any, company_id: Any, field_name: str, source: str, target: str) -> bool:
    rule_type = _rule_type_for_field(field_name)
    q = select(CorrectionRule).where(
        CorrectionRule.tenant_id == tenant_id,
        CorrectionRule.rule_type == rule_type,
        CorrectionRule.field_name == field_name,
        CorrectionRule.source_pattern == source,
        CorrectionRule.target_value == target,
        CorrectionRule.active.is_(True),
        CorrectionRule.is_global.is_(False),
    )
    if company_id is None:
        q = q.where(CorrectionRule.company_id.is_(None))
    else:
        q = q.where(or_(CorrectionRule.company_id == company_id, CorrectionRule.company_id.is_(None)))
    return db.execute(q.limit(1)).scalar_one_or_none() is not None


def _existing_open_proposal(db: Session, fingerprint: str) -> LearningRecommendationProposal | None:
    return db.execute(
        select(LearningRecommendationProposal)
        .where(
            LearningRecommendationProposal.fingerprint == fingerprint,
            LearningRecommendationProposal.status.in_(
                (
                    LEARNING_PROPOSAL_STATUS_PROPOSED,
                    LEARNING_PROPOSAL_STATUS_APPROVED,
                    LEARNING_PROPOSAL_STATUS_PROMOTED,
                )
            ),
        )
        .limit(1)
    ).scalar_one_or_none()


def _candidate_groups(db: Session, *, tenant_id: Any, company_id: Any | None) -> dict[tuple[str, str], list[InvoiceFieldCandidate]]:
    q = select(InvoiceFieldCandidate).where(
        InvoiceFieldCandidate.tenant_id == tenant_id,
        InvoiceFieldCandidate.field_name.in_(tuple(RECOMMENDABLE_RULE_FIELDS)),
        InvoiceFieldCandidate.finalised_at.isnot(None),
        InvoiceFieldCandidate.final_value.isnot(None),
        InvoiceFieldCandidate.user_corrected.is_(True),
        InvoiceFieldCandidate.outcome_source.in_(tuple(TRUSTED_OUTCOME_SOURCES)),
    )
    if company_id is None:
        q = q.where(InvoiceFieldCandidate.company_id.is_(None))
    else:
        q = q.where(InvoiceFieldCandidate.company_id == company_id)

    groups: dict[tuple[str, str], list[InvoiceFieldCandidate]] = defaultdict(list)
    for cand in db.execute(q).scalars().all():
        row = db.get(M.InvoiceRow, cand.row_id)
        if row is None:
            continue
        batch = db.get(M.InvoiceBatch, cand.batch_id)
        if batch is None or not DEFAULT_EXPORT_ELIGIBILITY_POLICY.row_is_eligible_for_trusted_learning(db, batch=batch, row=row):
            continue
        source = cand.normalised_value or normalise_outcome_value(cand.field_name, cand.candidate_value)
        target = normalise_outcome_value(cand.field_name, cand.final_value)
        if not source or not target or source == target:
            continue
        groups[(cand.field_name, source)].append(cand)
    return groups


def _create_rule_proposal(
    db: Session,
    *,
    run: LearningRecommendationRun,
    field_name: str,
    source: str,
    target: str,
    candidates: list[InvoiceFieldCandidate],
) -> LearningRecommendationProposal | None:
    by_target: dict[str, list[InvoiceFieldCandidate]] = defaultdict(list)
    for cand in candidates:
        by_target[normalise_outcome_value(cand.field_name, cand.final_value)].append(cand)
    winning_target, support = max(by_target.items(), key=lambda item: len(item[1]))
    conflict_count = sum(len(v) for k, v in by_target.items() if k != winning_target)
    if winning_target != target or len(support) < 2 or conflict_count:
        return None
    target_display = str(support[0].final_value or "").strip()
    if not target_display:
        return None
    if _active_rule_exists(
        db,
        tenant_id=run.tenant_id,
        company_id=run.company_id,
        field_name=field_name,
        source=source,
        target=target_display,
    ):
        return None

    payload = {
        "rule_type": _rule_type_for_field(field_name),
        "field_name": field_name,
        "source_pattern": source,
        "target_value": target_display,
        "active": True,
    }
    fingerprint = _json_key({
        "tenant_id": str(run.tenant_id),
        "company_id": str(run.company_id) if run.company_id else None,
        "proposal_type": "correction_rule",
        "payload": payload,
    })
    if _existing_open_proposal(db, fingerprint):
        return None

    support_count = len(support)
    confidence = min(0.92, 0.62 + (support_count * 0.06))
    proposal = LearningRecommendationProposal(
        run_id=run.id,
        tenant_id=run.tenant_id,
        company_id=run.company_id,
        proposal_type="correction_rule",
        target_entity_type="correction_rule",
        title=_proposal_title(field_name, source, target_display),
        summary=(
            f"{support_count} trusted correction outcome(s) repeatedly changed "
            f"{field_name} from the same normalised source to the same final value."
        ),
        proposed_payload_json=payload,
        canary_scope_json={"tenant_id": str(run.tenant_id), "company_id": str(run.company_id) if run.company_id else None},
        evidence_summary_json={"support_count": support_count, "conflict_count": conflict_count},
        confidence=confidence,
        quality_score=confidence,
        latency_score=1.0,
        cost_score=1.0,
        risk_level="medium",
        fingerprint=fingerprint,
    )
    db.add(proposal)
    db.flush()

    for cand in support:
        db.add(LearningRecommendationEvidence(
            proposal_id=proposal.id,
            evidence_type="accepted_candidate_correction",
            source_table="invoice_field_candidates",
            source_id=str(cand.id),
            batch_id=cand.batch_id,
            scan_run_id=cand.scan_run_id,
            row_id=cand.row_id,
            field_name=cand.field_name,
            observed_value=cand.candidate_value,
            expected_value=cand.final_value,
            evidence_json={
                "source_type": cand.source_type,
                "source_id": cand.source_id,
                "outcome_source": cand.outcome_source,
                "candidate_confidence": float(cand.confidence) if cand.confidence is not None else None,
            },
        ))
        db.add(LearningRecommendationReplayResult(
            proposal_id=proposal.id,
            batch_id=cand.batch_id,
            scan_run_id=cand.scan_run_id,
            row_id=cand.row_id,
            replay_status="would_apply",
            changed_fields_json=[field_name],
            before_json={field_name: cand.candidate_value},
            after_json={field_name: cand.final_value},
            quality_score=confidence,
            latency_ms=0,
            cost_estimate=0,
            evidence_json={"mode": "deterministic_dry_run", "mutated_row": False},
        ))
    return proposal


def _create_dead_region_proposals(db: Session, *, run: LearningRecommendationRun) -> list[LearningRecommendationProposal]:
    q = select(RemapHint).where(
        RemapHint.tenant_id == run.tenant_id,
        RemapHint.active.is_(True),
        RemapHint.archived.is_(False),
        RemapHint.deleted_at.is_(None),
        RemapHint.failure_count >= 3,
        RemapHint.success_count == 0,
    )
    if run.company_id is None:
        q = q.where(RemapHint.company_id.is_(None))
    else:
        q = q.where(or_(RemapHint.company_id == run.company_id, RemapHint.company_id.is_(None)))

    proposals: list[LearningRecommendationProposal] = []
    for hint in db.execute(q).scalars().all():
        payload = {"remap_hint_id": hint.id, "action": "archive_dead_region"}
        fingerprint = _json_key({
            "tenant_id": str(run.tenant_id),
            "company_id": str(hint.company_id) if hint.company_id else None,
            "proposal_type": "dead_saved_region",
            "payload": payload,
        })
        if _existing_open_proposal(db, fingerprint):
            continue
        proposal = LearningRecommendationProposal(
            run_id=run.id,
            tenant_id=run.tenant_id,
            company_id=hint.company_id,
            proposal_type="dead_saved_region",
            target_entity_type="remap_hint",
            title="Recommend archiving saved region with repeated failures",
            summary="Saved region has repeated failed replays and no successful replay telemetry.",
            proposed_payload_json=payload,
            canary_scope_json={"tenant_id": str(run.tenant_id), "company_id": str(hint.company_id) if hint.company_id else None},
            evidence_summary_json={"failure_count": hint.failure_count, "success_count": hint.success_count},
            confidence=0.74,
            quality_score=0.74,
            latency_score=1.0,
            cost_score=1.0,
            risk_level="low",
            fingerprint=fingerprint,
        )
        db.add(proposal)
        db.flush()
        db.add(LearningRecommendationEvidence(
            proposal_id=proposal.id,
            evidence_type="saved_region_telemetry",
            source_table="remap_hints",
            source_id=str(hint.id),
            batch_id=hint.last_used_batch_id,
            row_id=hint.last_used_row_id,
            field_name=hint.field_name,
            observed_value=hint.last_result,
            expected_value="successful saved-region replay",
            evidence_json={
                "failure_count": hint.failure_count,
                "success_count": hint.success_count,
                "conflict_count": hint.conflict_count,
                "apply_count": hint.apply_count,
            },
        ))
        db.add(LearningRecommendationReplayResult(
            proposal_id=proposal.id,
            batch_id=hint.last_used_batch_id,
            row_id=hint.last_used_row_id,
            replay_status="would_archive",
            changed_fields_json=["remap_hint.active", "remap_hint.archived"],
            before_json={"active": hint.active, "archived": hint.archived},
            after_json={"active": False, "archived": True},
            quality_score=0.74,
            latency_ms=0,
            cost_estimate=0,
            evidence_json={"mode": "telemetry_review", "mutated_hint": False},
        ))
        proposals.append(proposal)
    return proposals


def run_learning_recommendation_agent(
    db: Session,
    *,
    tenant_id: Any,
    company_id: Any | None = None,
    requested_by: Any | None = None,
) -> LearningRecommendationRun:
    run = LearningRecommendationRun(
        tenant_id=tenant_id,
        company_id=company_id,
        requested_by=requested_by,
        status=LEARNING_RUN_STATUS_RUNNING,
        settings_json={"min_support": 2, "proposal_mode": "review_required"},
    )
    db.add(run)
    db.flush()
    try:
        proposals: list[LearningRecommendationProposal] = []
        for (field_name, source), candidates in _candidate_groups(db, tenant_id=tenant_id, company_id=company_id).items():
            by_target: dict[str, list[InvoiceFieldCandidate]] = defaultdict(list)
            for cand in candidates:
                by_target[normalise_outcome_value(cand.field_name, cand.final_value)].append(cand)
            if not by_target:
                continue
            target, support = max(by_target.items(), key=lambda item: len(item[1]))
            proposal = _create_rule_proposal(
                db,
                run=run,
                field_name=field_name,
                source=source,
                target=target,
                candidates=candidates,
            )
            if proposal is not None:
                proposals.append(proposal)

        proposals.extend(_create_dead_region_proposals(db, run=run))
        db.flush()
        proposal_ids = [p.id for p in proposals]
        evidence_count = 0
        replay_count = 0
        if proposal_ids:
            evidence_count = db.query(LearningRecommendationEvidence).filter(
                LearningRecommendationEvidence.proposal_id.in_(proposal_ids)
            ).count()
            replay_count = db.query(LearningRecommendationReplayResult).filter(
                LearningRecommendationReplayResult.proposal_id.in_(proposal_ids)
            ).count()
        run.status = LEARNING_RUN_STATUS_COMPLETED
        run.proposals_created = len(proposals)
        run.evidence_count = evidence_count
        run.replay_count = replay_count
        run.summary_json = {
            "proposals_created": len(proposals),
            "evidence_count": evidence_count,
            "replay_count": replay_count,
        }
        run.completed_at = _utcnow()
    except Exception as exc:
        run.status = LEARNING_RUN_STATUS_FAILED
        run.last_error = str(exc)[:4000]
        run.completed_at = _utcnow()
        raise
    return run


def approve_proposal(db: Session, proposal_id: int, *, user: Any, note: str | None = None) -> LearningRecommendationProposal:
    proposal = db.get(LearningRecommendationProposal, proposal_id)
    if proposal is None:
        raise LookupError("Proposal not found")
    if proposal.status != LEARNING_PROPOSAL_STATUS_PROPOSED:
        raise ValueError("Only proposed recommendations can be approved")
    proposal.status = LEARNING_PROPOSAL_STATUS_APPROVED
    proposal.decided_by = getattr(user, "id", None)
    proposal.decided_at = _utcnow()
    proposal.decision_note = note
    proposal.updated_at = _utcnow()
    db.add(LearningReviewerDecision(
        proposal_id=proposal.id,
        decision=LEARNING_DECISION_APPROVED,
        reviewer_id=getattr(user, "id", None),
        note=note,
    ))
    return proposal


def reject_proposal(db: Session, proposal_id: int, *, user: Any, note: str | None = None) -> LearningRecommendationProposal:
    proposal = db.get(LearningRecommendationProposal, proposal_id)
    if proposal is None:
        raise LookupError("Proposal not found")
    if proposal.status not in {LEARNING_PROPOSAL_STATUS_PROPOSED, LEARNING_PROPOSAL_STATUS_APPROVED}:
        raise ValueError("Only unpromoted recommendations can be rejected")
    proposal.status = LEARNING_PROPOSAL_STATUS_REJECTED
    proposal.decided_by = getattr(user, "id", None)
    proposal.decided_at = _utcnow()
    proposal.decision_note = note
    proposal.updated_at = _utcnow()
    db.add(LearningReviewerDecision(
        proposal_id=proposal.id,
        decision=LEARNING_DECISION_REJECTED,
        reviewer_id=getattr(user, "id", None),
        note=note,
    ))
    return proposal


def _rule_state(rule: CorrectionRule | None) -> dict[str, Any]:
    if rule is None:
        return {"existed": False}
    return {
        "existed": True,
        "id": rule.id,
        "tenant_id": str(rule.tenant_id),
        "company_id": str(rule.company_id) if rule.company_id else None,
        "rule_type": rule.rule_type,
        "field_name": rule.field_name,
        "source_pattern": rule.source_pattern,
        "target_value": rule.target_value,
        "active": rule.active,
        "disabled_by": str(rule.disabled_by) if rule.disabled_by else None,
        "disabled_at": rule.disabled_at.isoformat() if rule.disabled_at else None,
    }


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _hint_state(hint: RemapHint | None) -> dict[str, Any]:
    if hint is None:
        return {"existed": False}
    return {
        "existed": True,
        "id": hint.id,
        "active": hint.active,
        "archived": hint.archived,
        "archived_by": str(hint.archived_by) if hint.archived_by else None,
        "archived_at": hint.archived_at.isoformat() if hint.archived_at else None,
    }


def promote_proposal(db: Session, proposal_id: int, *, user: Any) -> LearningPromotion:
    proposal = db.get(LearningRecommendationProposal, proposal_id)
    if proposal is None:
        raise LookupError("Proposal not found")
    if proposal.status != LEARNING_PROPOSAL_STATUS_APPROVED:
        raise ValueError("Only approved recommendations can be promoted")

    payload = proposal.proposed_payload_json or {}
    user_id = getattr(user, "id", None)
    now = _utcnow()
    if proposal.proposal_type == "correction_rule":
        q = select(CorrectionRule).where(
            CorrectionRule.tenant_id == proposal.tenant_id,
            CorrectionRule.rule_type == payload["rule_type"],
            CorrectionRule.field_name == payload["field_name"],
            CorrectionRule.source_pattern == payload["source_pattern"],
            CorrectionRule.target_value == payload["target_value"],
            CorrectionRule.is_global.is_(False),
        )
        if proposal.company_id is None:
            q = q.where(CorrectionRule.company_id.is_(None))
        else:
            q = q.where(CorrectionRule.company_id == proposal.company_id)
        rule = db.execute(q.limit(1)).scalar_one_or_none()
        previous = _rule_state(rule)
        if rule is None:
            rule = CorrectionRule(
                tenant_id=proposal.tenant_id,
                company_id=proposal.company_id,
                rule_type=payload["rule_type"],
                field_name=payload["field_name"],
                source_pattern=payload["source_pattern"],
                target_value=payload["target_value"],
                active=True,
                created_by=user_id,
            )
            db.add(rule)
            db.flush()
        else:
            rule.active = True
            rule.disabled_by = None
            rule.disabled_at = None
        promoted = _rule_state(rule)
        promotion = LearningPromotion(
            proposal_id=proposal.id,
            promoted_entity_type="correction_rule",
            promoted_entity_id=str(rule.id),
            previous_state_json=previous,
            promoted_state_json=promoted,
            rollback_state_json=previous,
            promoted_by=user_id,
            promoted_at=now,
        )
    elif proposal.proposal_type == "dead_saved_region":
        hint = db.get(RemapHint, int(payload["remap_hint_id"]))
        if hint is None or hint.tenant_id != proposal.tenant_id:
            raise LookupError("Saved region not found for proposal scope")
        previous = _hint_state(hint)
        hint.active = False
        hint.archived = True
        hint.archived_by = user_id
        hint.archived_at = now
        promoted = _hint_state(hint)
        promotion = LearningPromotion(
            proposal_id=proposal.id,
            promoted_entity_type="remap_hint",
            promoted_entity_id=str(hint.id),
            previous_state_json=previous,
            promoted_state_json=promoted,
            rollback_state_json=previous,
            promoted_by=user_id,
            promoted_at=now,
        )
    else:
        raise ValueError(f"Unsupported proposal type: {proposal.proposal_type}")

    proposal.status = LEARNING_PROPOSAL_STATUS_PROMOTED
    proposal.updated_at = now
    db.add(promotion)
    return promotion


def rollback_promotion(db: Session, promotion_id: int, *, user: Any) -> LearningPromotion:
    promotion = db.get(LearningPromotion, promotion_id)
    if promotion is None:
        raise LookupError("Promotion not found")
    if promotion.rollback_status == "rolled_back":
        return promotion
    previous = promotion.rollback_state_json or {}
    if promotion.promoted_entity_type == "correction_rule":
        rule = db.get(CorrectionRule, int(promotion.promoted_entity_id))
        if rule is None:
            raise LookupError("Promoted rule no longer exists")
        if previous.get("existed"):
            rule.active = bool(previous.get("active"))
            rule.source_pattern = previous.get("source_pattern") or rule.source_pattern
            rule.target_value = previous.get("target_value") or rule.target_value
            rule.company_id = _uuid_or_none(previous.get("company_id"))
        else:
            rule.active = False
            rule.disabled_by = getattr(user, "id", None)
            rule.disabled_at = _utcnow()
    elif promotion.promoted_entity_type == "remap_hint":
        hint = db.get(RemapHint, int(promotion.promoted_entity_id))
        if hint is None:
            raise LookupError("Promoted saved region no longer exists")
        hint.active = bool(previous.get("active"))
        hint.archived = bool(previous.get("archived"))
        if not hint.archived:
            hint.archived_at = None
            hint.archived_by = None
    else:
        raise ValueError(f"Unsupported promotion entity: {promotion.promoted_entity_type}")

    promotion.rollback_status = "rolled_back"
    promotion.rolled_back_by = getattr(user, "id", None)
    promotion.rolled_back_at = _utcnow()
    proposal = db.get(LearningRecommendationProposal, promotion.proposal_id)
    if proposal is not None:
        proposal.status = LEARNING_PROPOSAL_STATUS_ROLLED_BACK
        proposal.updated_at = _utcnow()
    return promotion

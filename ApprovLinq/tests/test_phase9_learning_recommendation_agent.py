from __future__ import annotations

from datetime import datetime
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceRow, Tenant, User
import app.db.review_models  # noqa: F401  # registers review tables
import app.db.learning_models  # noqa: F401  # registers learning tables
from app.db.learning_models import (
    LearningPromotion,
    LearningRecommendationEvidence,
    LearningRecommendationProposal,
    LearningRecommendationReplayResult,
)
from app.db.review_models import CorrectionRule, InvoiceFieldCandidate, RemapHint
from app.services.learning_recommendation_agent import (
    approve_proposal,
    promote_proposal,
    rollback_promotion,
    run_learning_recommendation_agent,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _tenant_company_user(db, *, code="P9"):
    tenant = Tenant(id=uuid.uuid4(), tenant_code=code, tenant_name=f"{code} Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code=f"{code}C", company_name=f"{code} Co")
    user = User(
        id=uuid.uuid4(),
        email=f"{code.lower()}@example.test",
        full_name="Reviewer",
        password_hash="test",
        role="tenant_user",
        is_active=True,
    )
    db.add_all([tenant, company, user])
    db.flush()
    return tenant, company, user


def _batch_row(db, tenant, company, *, row_id_suffix: str):
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name=f"Batch {row_id_suffix}",
        status="exported",
    )
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        source_filename=f"{row_id_suffix}.pdf",
        page_no=1,
        supplier_name="Raw Supplier",
        invoice_number=f"INV-{row_id_suffix}",
        description="Goods",
        total_amount=10.0,
    )
    db.add_all([batch, row])
    db.flush()
    return batch, row


def _corrected_candidate(db, tenant, company, *, row_id_suffix: str, final_value="Approved Supplier Ltd"):
    batch, row = _batch_row(db, tenant, company, row_id_suffix=row_id_suffix)
    cand = InvoiceFieldCandidate(
        tenant_id=tenant.id,
        company_id=company.id,
        batch_id=batch.id,
        row_id=row.id,
        field_name="supplier_name",
        candidate_value="Raw Supplier",
        normalised_value="raw supplier",
        source_type="azure_di",
        confidence=0.72,
        selected=True,
        applied=True,
        user_corrected=True,
        final_value=final_value,
        finalised_at=datetime.utcnow(),
        outcome_source="export",
    )
    db.add(cand)
    db.flush()
    return batch, row, cand


def test_trusted_outcomes_create_evidence_backed_rule_proposal_without_mutating_rules(db):
    tenant, company, user = _tenant_company_user(db)
    _corrected_candidate(db, tenant, company, row_id_suffix="1")
    _corrected_candidate(db, tenant, company, row_id_suffix="2")
    db.commit()

    run = run_learning_recommendation_agent(db, tenant_id=tenant.id, company_id=company.id, requested_by=user.id)
    db.commit()

    assert run.status == "completed"
    assert run.proposals_created == 1
    proposal = db.execute(select(LearningRecommendationProposal)).scalar_one()
    assert proposal.status == "proposed"
    assert proposal.proposal_type == "correction_rule"
    assert proposal.proposed_payload_json["rule_type"] == "supplier_alias"
    assert proposal.proposed_payload_json["source_pattern"] == "raw supplier"
    assert proposal.proposed_payload_json["target_value"] == "Approved Supplier Ltd"
    assert db.query(LearningRecommendationEvidence).filter_by(proposal_id=proposal.id).count() == 2
    assert db.query(LearningRecommendationReplayResult).filter_by(proposal_id=proposal.id).count() == 2
    assert db.query(CorrectionRule).count() == 0


def test_unreviewed_and_cross_tenant_candidates_are_not_used(db):
    tenant, company, user = _tenant_company_user(db, code="P9A")
    other_tenant, other_company, _other_user = _tenant_company_user(db, code="P9B")
    _corrected_candidate(db, other_tenant, other_company, row_id_suffix="other-1")
    _corrected_candidate(db, other_tenant, other_company, row_id_suffix="other-2")
    _batch, row = _batch_row(db, tenant, company, row_id_suffix="untrusted")
    db.add(InvoiceFieldCandidate(
        tenant_id=tenant.id,
        company_id=company.id,
        batch_id=row.batch_id,
        row_id=row.id,
        field_name="supplier_name",
        candidate_value="Unreviewed Supplier",
        normalised_value="unreviewed supplier",
        source_type="azure_di",
        selected=True,
        applied=True,
        user_corrected=True,
        final_value="Should Not Propose",
        finalised_at=None,
        outcome_source=None,
    ))
    db.commit()

    run = run_learning_recommendation_agent(db, tenant_id=tenant.id, company_id=company.id, requested_by=user.id)
    db.commit()

    assert run.proposals_created == 0
    assert db.query(LearningRecommendationProposal).filter_by(tenant_id=tenant.id).count() == 0


def test_promotion_requires_approval_and_rollback_disables_created_rule(db):
    tenant, company, user = _tenant_company_user(db)
    _corrected_candidate(db, tenant, company, row_id_suffix="1")
    _corrected_candidate(db, tenant, company, row_id_suffix="2")
    run_learning_recommendation_agent(db, tenant_id=tenant.id, company_id=company.id, requested_by=user.id)
    proposal = db.execute(select(LearningRecommendationProposal)).scalar_one()

    with pytest.raises(ValueError):
        promote_proposal(db, proposal.id, user=user)

    approve_proposal(db, proposal.id, user=user, note="Looks safe")
    promotion = promote_proposal(db, proposal.id, user=user)
    db.commit()

    rule = db.get(CorrectionRule, int(promotion.promoted_entity_id))
    assert rule is not None
    assert rule.active is True
    assert rule.rule_type == "supplier_alias"
    assert db.get(LearningPromotion, promotion.id).previous_state_json["existed"] is False

    rollback_promotion(db, promotion.id, user=user)
    db.commit()

    assert rule.active is False
    assert db.get(LearningPromotion, promotion.id).rollback_status == "rolled_back"
    assert db.get(LearningRecommendationProposal, proposal.id).status == "rolled_back"


def test_dead_saved_region_proposal_can_be_promoted_and_rolled_back(db):
    tenant, company, user = _tenant_company_user(db)
    hint = RemapHint(
        id=1001,
        tenant_id=tenant.id,
        company_id=company.id,
        field_name="total_amount",
        supplier_name_snapshot="Any Supplier",
        active=True,
        archived=False,
        failure_count=4,
        success_count=0,
        apply_count=4,
    )
    db.add(hint)
    db.commit()

    run = run_learning_recommendation_agent(db, tenant_id=tenant.id, company_id=company.id, requested_by=user.id)
    proposal = db.execute(select(LearningRecommendationProposal)).scalar_one()
    assert run.proposals_created == 1
    assert proposal.proposal_type == "dead_saved_region"
    assert hint.active is True
    assert hint.archived is False

    approve_proposal(db, proposal.id, user=user)
    promotion = promote_proposal(db, proposal.id, user=user)
    db.commit()

    assert hint.active is False
    assert hint.archived is True

    rollback_promotion(db, promotion.id, user=user)
    db.commit()

    assert hint.active is True
    assert hint.archived is False

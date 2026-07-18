from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, INVOICE_ROW_STATUS_ACTIVE, InvoiceBatch, InvoiceRow, Tenant, User, UserTenant
import app.db.review_models  # noqa: F401
import app.db.learning_models  # noqa: F401
import app.db.insight_models  # noqa: F401
from app.db.insight_models import ApprovedInvoiceFact, FxRate
from app.db.learning_models import (
    LEARNING_CANARY_STATUS_PASSED,
    LEARNING_PROPOSAL_STATUS_APPROVED,
    LearningPromotion,
    LearningRecommendationProposal,
    LearningRecommendationReplayResult,
    LearningRecommendationRun,
)
from app.db.review_models import BatchExportEvent
from app.services import expense_insights
from app.services.approved_invoice_facts import materialise_approved_invoice_facts_for_export
from app.services.fx_rates import resolve_fx_rate_snapshot
from app.services.learning_governance import LearningGovernanceError, require_learning_permission
from app.services.learning_recommendation_agent import approve_proposal, mark_canary_passed, promote_proposal


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _tenant_company_users(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="P14", tenant_name="Phase 14 Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="P14C", company_name="Phase 14 Co")
    users = [
        User(id=uuid.uuid4(), email=f"p14-{name}@example.test", full_name=name, password_hash="x", role="tenant_user")
        for name in ("requester", "approver", "operator", "viewer")
    ]
    db.add_all([tenant, company, *users])
    db.flush()
    for user, role in zip(users, ("learning_admin", "learning_approver", "learning_operator", "learning_viewer")):
        db.add(UserTenant(user_id=user.id, tenant_id=tenant.id, tenant_role=role, is_default=True))
    db.flush()
    return tenant, company, users


def _proposal(db, tenant, company, requester):
    run = LearningRecommendationRun(tenant_id=tenant.id, company_id=company.id, requested_by=requester.id, status="completed")
    db.add(run)
    db.flush()
    proposal = LearningRecommendationProposal(
        run_id=run.id,
        tenant_id=tenant.id,
        company_id=company.id,
        proposal_type="correction_rule",
        target_entity_type="correction_rule",
        title="Recommend supplier alias",
        proposed_payload_json={
            "rule_type": "supplier_alias",
            "field_name": "supplier_name",
            "source_pattern": "raw supplier",
            "target_value": "Approved Supplier Ltd",
            "active": True,
        },
        canary_scope_json={"tenant_id": str(tenant.id), "company_id": str(company.id)},
        fingerprint=uuid.uuid4().hex,
    )
    db.add(proposal)
    db.flush()
    db.add(LearningRecommendationReplayResult(
        proposal_id=proposal.id,
        replay_status="would_apply",
        changed_fields_json=["supplier_name"],
        before_json={"supplier_name": "Raw Supplier"},
        after_json={"supplier_name": "Approved Supplier Ltd"},
    ))
    db.flush()
    return proposal


def test_learning_governance_enforces_rbac_separation_canary_and_rollback_controls(db):
    tenant, company, (requester, approver, operator, viewer) = _tenant_company_users(db)
    proposal = _proposal(db, tenant, company, requester)

    require_learning_permission(db, user=requester, tenant_id=tenant.id, action="run")
    require_learning_permission(db, user=approver, tenant_id=tenant.id, action="approve")
    require_learning_permission(db, user=operator, tenant_id=tenant.id, action="promote")
    with pytest.raises(LearningGovernanceError):
        require_learning_permission(db, user=viewer, tenant_id=tenant.id, action="promote")

    with pytest.raises(LearningGovernanceError):
        approve_proposal(db, proposal.id, user=requester)

    approve_proposal(db, proposal.id, user=approver, note="Reviewed evidence")
    with pytest.raises(ValueError, match="pass canary"):
        promote_proposal(db, proposal.id, user=operator)

    with pytest.raises(LearningGovernanceError):
        mark_canary_passed(db, proposal.id, user=approver)

    mark_canary_passed(db, proposal.id, user=operator, note="Replay evidence clean")
    assert proposal.canary_status == LEARNING_CANARY_STATUS_PASSED
    with pytest.raises(LearningGovernanceError):
        promote_proposal(db, proposal.id, user=approver)

    promotion = promote_proposal(db, proposal.id, user=requester)
    db.flush()

    persisted = db.get(LearningPromotion, promotion.id)
    assert persisted is not None
    assert persisted.rollback_state_json["existed"] is False
    assert persisted.canary_snapshot_json["canary_status"] == "passed"


def test_exact_calendar_window_uses_month_boundaries():
    start, end = expense_insights.exact_month_window(3, today=date(2026, 7, 18))
    assert start == date(2026, 5, 1)
    assert end == date(2026, 8, 1)


def test_sql_analytics_preserve_drilldown_and_narrative_contract(db):
    tenant, company, _users = _tenant_company_users(db)
    batch = InvoiceBatch(id=uuid.uuid4(), tenant_id=tenant.id, company_id=company.id, batch_name="Insights", status="exported")
    db.add(batch)
    db.flush()
    db.add_all([
        ApprovedInvoiceFact(
            tenant_id=tenant.id,
            company_id=company.id,
            batch_id=batch.id,
            source_row_id=1,
            export_version=1,
            fact_version=1,
            fact_fingerprint="f1",
            evidence_ref_type="batch_export_event",
            evidence_ref_id="1",
            canonical_supplier_name="Supplier A",
            document_type="invoice",
            invoice_number="A-1",
            invoice_date=date(2026, 7, 1),
            category="Utilities",
            reporting_net_amount=Decimal("100.00"),
            reporting_vat_amount=Decimal("18.00"),
            reporting_total_amount=Decimal("130.00"),
        ),
        ApprovedInvoiceFact(
            tenant_id=tenant.id,
            company_id=company.id,
            batch_id=batch.id,
            source_row_id=2,
            export_version=1,
            fact_version=1,
            fact_fingerprint="f2",
            evidence_ref_type="batch_export_event",
            evidence_ref_id="1",
            canonical_supplier_name="Supplier B",
            document_type="credit note",
            invoice_number="CN-1",
            invoice_date=date(2026, 7, 2),
            reporting_net_amount=Decimal("-50.00"),
            reporting_vat_amount=Decimal("-9.00"),
            reporting_total_amount=Decimal("-59.00"),
        ),
    ])
    db.commit()

    summary = expense_insights.approved_summary(db, tenant_id=tenant.id, company_id=company.id)
    vat = expense_insights.vat_exceptions(db, tenant_id=tenant.id, company_id=company.id)
    credits = expense_insights.credit_note_impact(db, tenant_id=tenant.id, company_id=company.id)

    assert summary["narrative_contract"]["facts"].startswith("Metrics are computed")
    assert vat["exception_count"] == 1
    assert vat["rows"][0]["fact_id"] == 1
    assert credits["credit_note_count"] == 1
    assert credits["credit_note_total"] == -59.0
    assert credits["narrative_contract"]["hypotheses"].startswith("Narrative observations")


def test_bcrs_fact_components_and_fx_provenance_are_materialised_from_export_snapshot(db):
    tenant, company, (user, *_rest) = _tenant_company_users(db)
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="BCRS export",
        status="exported",
        scan_mode="detail",
    )
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        source_filename="bcrs.pdf",
        page_no=1,
        supplier_name="Supplier",
        invoice_number="BCRS-1",
        invoice_date=date(2026, 7, 18),
        description="BCRS deposit",
        net_amount=Decimal("0.00"),
        vat_amount=Decimal("0.00"),
        total_amount=Decimal("2.40"),
        currency="EUR",
        confidence_score=Decimal("0.99"),
        validation_status="ok",
        review_required=False,
        row_status=INVOICE_ROW_STATUS_ACTIVE,
        method_used="bcrs_split",
    )
    db.add_all([batch, row])
    db.flush()
    event = BatchExportEvent(
        batch_id=batch.id,
        export_version=1,
        exported_by=user.id,
        exported_at=datetime.utcnow(),
        file_path="/tmp/bcrs.xlsx",
        file_bytes=b"export bytes",
        storage_backend="database+local",
        row_count=1,
    )
    db.add(event)
    db.commit()

    assert materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=event) == 1
    fact = db.execute(select(ApprovedInvoiceFact)).scalar_one()

    assert fact.bcrs_amount == Decimal("2.40")
    assert fact.deposit_component_type == "bcrs"
    assert fact.deposit_component_source == "bcrs_split"
    assert fact.deposit_component_provenance_json["source_row_id"] == row.id
    assert fact.fx_rate == Decimal("1.00000000")
    assert fact.fx_rate_source == "identity"
    assert fact.fx_rate_provenance_json["model"] == "identity"


def test_fx_rate_model_resolves_configured_rates_with_provenance(db):
    tenant, _company, _users = _tenant_company_users(db)
    db.add(FxRate(
        tenant_id=tenant.id,
        currency="GBP",
        reporting_currency="EUR",
        rate_date=date(2026, 7, 1),
        rate=Decimal("1.17000000"),
        source="finance_upload",
        source_reference="fx-2026-07",
        provenance_json={"uploaded_by": "finance"},
    ))
    db.commit()

    snapshot = resolve_fx_rate_snapshot(
        db,
        tenant_id=tenant.id,
        currency="GBP",
        reporting_currency="EUR",
        invoice_date=date(2026, 7, 18),
    )

    assert snapshot.rate == Decimal("1.17000000")
    assert snapshot.source == "finance_upload"
    assert snapshot.provenance["fx_rate_id"] is not None

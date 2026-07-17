from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, INVOICE_ROW_STATUS_ACTIVE, InvoiceBatch, InvoiceFile, InvoiceRow, Tenant, User
import app.db.review_models  # noqa: F401
import app.db.learning_models  # noqa: F401
import app.db.insight_models  # noqa: F401
from app.db.insight_models import ApprovedInvoiceFact
from app.db.review_models import BatchExportEvent, InvoiceFieldCandidate, InvoiceRowCorrection
from app.services.approved_invoice_facts import materialise_approved_invoice_facts_for_export
from app.services.candidate_outcomes import label_batch_candidates
from app.services.export_eligibility import ExportEligibilityError, ExportEligibilityPolicy
from app.services import expense_insights
import app.routers.batches as batches_router


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _tenant_company_user(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="P12", tenant_name="Phase 12 Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="P12C", company_name="Phase 12 Company")
    user = User(id=uuid.uuid4(), email="p12@example.test", full_name="Reviewer", password_hash="x", role="tenant_user")
    db.add_all([tenant, company, user])
    db.flush()
    return tenant, company, user


def _batch(db, tenant, company, *, status="approved"):
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="Phase 12 batch",
        status=status,
        scan_mode="summary",
    )
    db.add(batch)
    db.flush()
    return batch


def _row(db, tenant, company, batch, **overrides):
    data = {
        "batch_id": batch.id,
        "tenant_id": tenant.id,
        "company_id": company.id,
        "source_filename": "invoice.pdf",
        "page_no": 1,
        "supplier_name": "Supplier Ltd",
        "invoice_number": "INV-12",
        "invoice_date": date(2026, 7, 17),
        "description": "Approved supplies",
        "net_amount": Decimal("100.00"),
        "vat_amount": Decimal("18.00"),
        "total_amount": Decimal("118.00"),
        "currency": "EUR",
        "confidence_score": Decimal("0.91"),
        "validation_status": "ok",
        "review_required": False,
        "row_status": INVOICE_ROW_STATUS_ACTIVE,
    }
    data.update(overrides)
    row = InvoiceRow(**data)
    db.add(row)
    db.flush()
    return row


def _event(db, batch, user, *, version=1, row_count=1):
    event = BatchExportEvent(
        batch_id=batch.id,
        scan_run_id=batch.current_scan_run_id,
        export_version=version,
        exported_by=user.id,
        exported_at=datetime.utcnow(),
        file_path=f"/tmp/{batch.id}.xlsx",
        file_bytes=b"immutable export bytes",
        storage_backend="database+local",
        row_count=row_count,
    )
    db.add(event)
    db.flush()
    return event


def test_export_policy_blocks_unapproved_unresolved_and_page_error_rows(db):
    tenant, company, _user = _tenant_company_user(db)
    draft = _batch(db, tenant, company, status="in_review")
    _row(db, tenant, company, draft)
    result = ExportEligibilityPolicy().evaluate(db, draft)
    assert not result.eligible
    assert "batch_not_approved" in result.codes

    batch = _batch(db, tenant, company, status="approved")
    unresolved = _row(db, tenant, company, batch, review_required=True, review_reasons="supplier_unknown")
    page_error = _row(
        db,
        tenant,
        company,
        batch,
        invoice_number="ERR-1",
        method_used="page_error",
        validation_status="review",
        review_reasons="page_error",
    )

    result = ExportEligibilityPolicy().evaluate(db, batch)
    assert not result.eligible
    assert {issue.row_id for issue in result.issues if issue.row_id} == {unresolved.id, page_error.id}
    assert "row_unresolved" in result.codes
    assert "row_page_error" in result.codes


def test_fact_creation_requires_export_policy_and_export_event_snapshot(db):
    tenant, company, user = _tenant_company_user(db)
    batch = _batch(db, tenant, company, status="approved")
    _row(db, tenant, company, batch, review_required=True, review_reasons="needs_review")
    event = _event(db, batch, user)

    with pytest.raises(ExportEligibilityError):
        materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=event)


def test_archiving_batch_preserves_rows_exports_and_approved_facts(db, monkeypatch):
    tenant, company, user = _tenant_company_user(db)
    batch = _batch(db, tenant, company, status="exported")
    row = _row(db, tenant, company, batch)
    db.add(InvoiceFile(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        original_filename="invoice.pdf",
        stored_filename="invoice.pdf",
        file_path="/tmp/invoice.pdf",
        file_bytes=b"pdf bytes",
        storage_backend="database+local",
        status="processed",
    ))
    event = _event(db, batch, user)
    assert materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=event) == 1
    db.commit()

    monkeypatch.setattr(batches_router, "_ACTIVE_BATCHES", set())
    archived = batches_router.delete_batch(batch.id, db=db, tenant_id=tenant.id, _user=user)
    db.commit()

    assert archived["deleted"] is False
    assert archived["archived"] is True
    assert db.get(InvoiceBatch, batch.id).status == "archived"
    assert db.get(InvoiceRow, row.id) is not None
    assert db.get(BatchExportEvent, event.id) is not None
    assert db.query(ApprovedInvoiceFact).filter_by(batch_id=batch.id).count() == 1


def test_unresolved_rows_do_not_become_trusted_learning_labels(db):
    tenant, company, user = _tenant_company_user(db)
    batch = _batch(db, tenant, company, status="approved")
    row = _row(db, tenant, company, batch, review_required=True, review_reasons="supplier_unknown")
    candidate = InvoiceFieldCandidate(
        tenant_id=tenant.id,
        company_id=company.id,
        batch_id=batch.id,
        row_id=row.id,
        field_name="supplier_name",
        candidate_value="Wrong Supplier",
        normalised_value="wrong supplier",
        source_type="azure_di",
        selected=True,
    )
    db.add(candidate)
    db.flush()

    assert label_batch_candidates(db, batch=batch, user=user, outcome_source="export") == 0
    assert candidate.finalised_at is None


def test_insight_metric_drilldown_reconciles_to_immutable_facts(db):
    tenant, company, _user = _tenant_company_user(db)
    batch = _batch(db, tenant, company, status="archived")
    facts = [
        ApprovedInvoiceFact(
            tenant_id=tenant.id,
            company_id=company.id,
            batch_id=batch.id,
            source_row_id=1,
            export_version=1,
            fact_version=1,
            fact_fingerprint="p12-a",
            evidence_ref_type="batch_export_event",
            evidence_ref_id="10",
            canonical_supplier_name="Supplier A",
            invoice_number="A-1",
            invoice_date=date(2026, 7, 1),
            category="Supplies",
            reporting_net_amount=Decimal("100.00"),
            reporting_vat_amount=Decimal("18.00"),
            reporting_total_amount=Decimal("118.00"),
        ),
        ApprovedInvoiceFact(
            tenant_id=tenant.id,
            company_id=company.id,
            batch_id=batch.id,
            source_row_id=2,
            export_version=1,
            fact_version=1,
            fact_fingerprint="p12-b",
            evidence_ref_type="batch_export_event",
            evidence_ref_id="10",
            canonical_supplier_name="Supplier A",
            invoice_number="A-2",
            invoice_date=date(2026, 7, 2),
            category="Supplies",
            reporting_net_amount=Decimal("50.00"),
            reporting_vat_amount=Decimal("9.00"),
            reporting_total_amount=Decimal("59.00"),
        ),
    ]
    db.add_all(facts)
    db.commit()

    summary = expense_insights.approved_summary(db, tenant_id=tenant.id, company_id=company.id)
    top = expense_insights.top_suppliers(db, tenant_id=tenant.id, company_id=company.id)
    drilldown = expense_insights.fact_drilldown(db, tenant_id=tenant.id, company_id=company.id, fact_id=facts[0].id)

    assert summary["total_spend"] == 177.0
    assert round(sum(item["total"] for item in top), 2) == summary["total_spend"]
    assert set(top[0]["fact_ids"]) == {facts[0].id, facts[1].id}
    assert drilldown["fact"]["id"] == facts[0].id
    assert drilldown["evidence"]["evidence_ref_type"] == "batch_export_event"

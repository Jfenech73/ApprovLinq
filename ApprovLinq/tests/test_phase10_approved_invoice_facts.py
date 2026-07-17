from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Company,
    INVOICE_ROW_STATUS_ACTIVE,
    InvoiceBatch,
    InvoiceReadHeader,
    InvoiceRow,
    Tenant,
    TenantNominalAccount,
    User,
)
import app.db.review_models  # noqa: F401  # registers review/export tables
import app.db.learning_models  # noqa: F401  # registers learning tables
import app.db.insight_models  # noqa: F401  # registers insight tables
from app.db.insight_models import ApprovedInvoiceFact
from app.db.review_models import BatchExportEvent, InvoiceDuplicateCandidate, InvoiceRowCorrection
from app.services import expense_insights
from app.services.approved_invoice_facts import materialise_approved_invoice_facts_for_export


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _tenant_company_user(db, *, code: str = "P10"):
    tenant = Tenant(id=uuid.uuid4(), tenant_code=code, tenant_name=f"{code} Tenant")
    company = Company(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_code=f"{code}C",
        company_name=f"{code} Company",
    )
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


def _batch(db, tenant, company, *, name: str = "Approved batch"):
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name=name,
        status="exported",
        scan_mode="detail",
    )
    db.add(batch)
    db.flush()
    return batch


def _export_event(db, batch, user, *, event_id: int, version: int):
    ev = BatchExportEvent(
        id=event_id,
        batch_id=batch.id,
        scan_run_id=batch.current_scan_run_id,
        export_version=version,
        exported_by=user.id,
        exported_at=datetime.utcnow(),
        file_path=f"/tmp/batch-{batch.id}-v{version}.xlsx",
        file_bytes=b"test workbook bytes",
        storage_backend="database+local",
        row_count=1,
    )
    db.add(ev)
    db.flush()
    return ev


def _invoice_row(db, tenant, company, batch, **overrides):
    data = {
        "batch_id": batch.id,
        "tenant_id": tenant.id,
        "company_id": company.id,
        "source_filename": "invoice.pdf",
        "page_no": 1,
        "supplier_name": "Raw Supplier Ltd",
        "supplier_posting_account": "SUP-RAW",
        "nominal_account_code": "5000",
        "invoice_number": "INV-100",
        "invoice_date": date(2026, 7, 1),
        "description": "Raw OCR description that should be corrected",
        "net_amount": Decimal("100.00"),
        "vat_amount": Decimal("18.00"),
        "total_amount": Decimal("118.00"),
        "currency": "EUR",
        "tax_code": "VAT18",
        "confidence_score": Decimal("0.91"),
        "validation_status": "ok",
        "review_required": False,
        "row_status": INVOICE_ROW_STATUS_ACTIVE,
        "header_raw": "RAW HEADER SHOULD NOT BECOME AN INSIGHT FACT",
        "page_text_raw": "RAW PAGE TEXT SHOULD NOT BECOME AN INSIGHT FACT",
    }
    data.update(overrides)
    row = InvoiceRow(**data)
    db.add(row)
    db.flush()
    return row


def test_materialises_corrected_export_snapshot_and_excludes_blocked_rows(db):
    tenant, company, user = _tenant_company_user(db)
    db.add(TenantNominalAccount(
        tenant_id=tenant.id,
        company_id=company.id,
        account_code="5000",
        account_name="Office Supplies",
        is_active=True,
    ))
    batch = _batch(db, tenant, company)
    row = _invoice_row(db, tenant, company, batch)
    blocked = _invoice_row(
        db,
        tenant,
        company,
        batch,
        invoice_number="INV-BLOCKED",
        row_status="blocked_duplicate",
        row_status_reason="cross_batch_duplicate",
    )
    db.add(InvoiceRowCorrection(
        row_id=row.id,
        batch_id=batch.id,
        supplier_name="Corrected Supplier Ltd",
        supplier_posting_account="SUP-CORR",
        description="Corrected purchase summary",
        total_amount=Decimal("121.00"),
        reviewed_fields="supplier_name,total_amount,description",
        row_reviewed=True,
        updated_by=user.id,
    ))
    db.add(InvoiceReadHeader(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        row_id=row.id,
        source_filename="invoice.pdf",
        page_no=1,
        provider_name="azure_di",
        document_type="invoice",
        supplier_vat="MT12345678",
    ))
    db.add(InvoiceDuplicateCandidate(
        tenant_id=tenant.id,
        company_id=company.id,
        batch_id=batch.id,
        row_id=row.id,
        candidate_batch_id=uuid.uuid4(),
        candidate_row_id=999,
        match_type="cross_batch",
        match_status="review_only",
        confidence=Decimal("0.84"),
    ))
    ev = _export_event(db, batch, user, event_id=1, version=1)
    db.commit()

    created = materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=ev)
    db.commit()

    assert created == 1
    facts = db.execute(select(ApprovedInvoiceFact)).scalars().all()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.source_row_id == row.id
    assert fact.source_row_id != blocked.id
    assert fact.canonical_supplier_name == "Corrected Supplier Ltd"
    assert fact.supplier_posting_account == "SUP-CORR"
    assert fact.total_amount == Decimal("121.00")
    assert fact.description == "Corrected purchase summary"
    assert fact.nominal_account_name == "Office Supplies"
    assert fact.category == "Office Supplies"
    assert fact.supplier_vat == "MT12345678"
    assert fact.document_type == "invoice"
    assert fact.duplicate_exposure_count == 1
    assert fact.duplicate_exposure_status == "review_only"
    assert "RAW HEADER" not in (fact.description or "")
    assert "RAW PAGE" not in (fact.description or "")

    assert materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=ev) == 0


def test_insights_use_only_latest_approved_fact_version_not_mutable_rows(db):
    tenant, company, user = _tenant_company_user(db, code="P10B")
    batch = _batch(db, tenant, company)
    row = _invoice_row(
        db,
        tenant,
        company,
        batch,
        supplier_name="Initial Supplier",
        total_amount=Decimal("100.00"),
        net_amount=Decimal("90.00"),
        vat_amount=Decimal("10.00"),
        confidence_score=Decimal("0.80"),
    )
    ev1 = _export_event(db, batch, user, event_id=11, version=1)
    assert materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=ev1) == 1
    db.commit()

    correction = InvoiceRowCorrection(
        row_id=row.id,
        batch_id=batch.id,
        supplier_name="Final Supplier",
        total_amount=Decimal("200.00"),
        net_amount=Decimal("170.00"),
        vat_amount=Decimal("30.00"),
        reviewed_fields="supplier_name,total_amount,net_amount,vat_amount",
        row_reviewed=True,
        updated_by=user.id,
    )
    db.add(correction)
    ev2 = _export_event(db, batch, user, event_id=12, version=2)
    assert materialise_approved_invoice_facts_for_export(db, batch=batch, export_event=ev2) == 1
    db.commit()

    row.total_amount = Decimal("9999.00")
    row.supplier_name = "Mutable Row Supplier"
    db.commit()

    summary = expense_insights.approved_summary(db, tenant_id=tenant.id, company_id=company.id)
    top = expense_insights.top_suppliers(db, tenant_id=tenant.id, company_id=company.id)
    monthly = expense_insights.approved_monthly(db, tenant_id=tenant.id, company_id=company.id, months=12)

    assert db.query(ApprovedInvoiceFact).filter_by(batch_id=batch.id).count() == 2
    assert summary["total_rows"] == 1
    assert summary["total_spend"] == 200.0
    assert top == [{"supplier_name": "Final Supplier", "total": 200.0, "count": 1, "fact_ids": [2]}]
    assert monthly[-1]["total"] == 200.0


def test_expense_insights_are_computed_from_approved_facts(db):
    tenant, company, _user = _tenant_company_user(db, code="P10C")
    batch = _batch(db, tenant, company)
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
            duplicate_exposure_count=1,
            duplicate_exposure_status="review_only",
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
            category="Supplies",
            reporting_net_amount=Decimal("-50.00"),
            reporting_vat_amount=Decimal("-9.00"),
            reporting_total_amount=Decimal("-59.00"),
        ),
    ])
    db.commit()

    categories = expense_insights.category_spend(db, tenant_id=tenant.id, company_id=company.id)
    duplicates = expense_insights.duplicate_exposure(db, tenant_id=tenant.id, company_id=company.id)
    vat = expense_insights.vat_exceptions(db, tenant_id=tenant.id, company_id=company.id)
    credits = expense_insights.credit_note_impact(db, tenant_id=tenant.id, company_id=company.id)

    assert categories[0] == {"category": "Utilities", "total": 130.0, "count": 1, "fact_ids": [1]}
    assert duplicates["exposed_count"] == 1
    assert duplicates["exposed_total"] == 130.0
    assert vat["exception_count"] == 1
    assert vat["rows"][0]["difference"] == 12.0
    assert credits["credit_note_count"] == 1
    assert credits["credit_note_total"] == -59.0

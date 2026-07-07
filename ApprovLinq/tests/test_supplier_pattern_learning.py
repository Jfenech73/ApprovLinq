from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceRow, SupplierPattern, Tenant, TenantSupplier, User
import app.db.review_models  # noqa: F401  # registers review/correction tables
from app.db.review_models import InvoiceRowCorrection
from app.services.supplier_pattern_learning import (
    match_supplier_by_active_pattern,
    promote_supplier_pattern_from_row,
    record_supplier_pattern_proposals_for_batch,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    table_names = [
        "tenants",
        "users",
        "companies",
        "tenant_suppliers",
        "invoice_batches",
        "invoice_rows",
        "invoice_row_corrections",
        "invoice_row_field_audits",
        "supplier_patterns",
    ]
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in table_names])
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def tenant_company_supplier(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="T1", tenant_name="Tenant One")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="C1", company_name="Company One")
    supplier = TenantSupplier(
        tenant_id=tenant.id,
        company_id=company.id,
        supplier_account_code="SUP-001",
        supplier_name="Example Supplier Ltd",
        posting_account="SUP-001",
        is_active=True,
    )
    user = User(
        id=uuid.uuid4(),
        email="reviewer@example.test",
        full_name="Reviewer",
        password_hash="test",
        role="tenant_user",
        is_active=True,
    )
    db.add_all([tenant, company, supplier, user])
    db.flush()
    return tenant, company, supplier, user


def _batch_and_row(db, tenant, company, *, supplier_name="Example Supplier Ltd", header_raw: str | None = None):
    batch = InvoiceBatch(id=uuid.uuid4(), tenant_id=tenant.id, company_id=company.id, batch_name="Batch", status="processed")
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        source_filename="invoice.pdf",
        page_no=1,
        supplier_name=supplier_name,
        header_raw=header_raw or "Example Supplier Wholesale Trading Warehouse Supplies",
    )
    db.add_all([batch, row])
    db.flush()
    return batch, row


def test_scan_completion_records_proposed_pattern_not_active(db, tenant_company_supplier):
    tenant, company, supplier, _user = tenant_company_supplier
    batch, _row = _batch_and_row(db, tenant, company)

    from app.routers.batches import _learn_supplier_patterns

    _learn_supplier_patterns(batch.id, tenant.id, company.id, db)

    pattern = db.query(SupplierPattern).filter_by(supplier_id=supplier.id).one()
    assert pattern.status == "proposed"
    assert pattern.keywords is None
    assert pattern.proposed_keywords
    assert pattern.proposal_count == 1
    assert db.query(SupplierPattern).filter_by(status="active").count() == 0


def test_unreviewed_scan_does_not_merge_into_existing_active_keywords(db, tenant_company_supplier):
    tenant, company, supplier, _user = tenant_company_supplier
    active = SupplierPattern(
        tenant_id=tenant.id,
        company_id=company.id,
        supplier_id=supplier.id,
        keywords="alpha beta gamma",
        hit_count=3,
        status="active",
    )
    db.add(active)
    batch, _row = _batch_and_row(db, tenant, company, header_raw="Untrusted Extra Header Tokens")
    db.commit()

    record_supplier_pattern_proposals_for_batch(db, batch_id=batch.id, tenant_id=tenant.id, company_id=company.id)
    db.commit()
    db.refresh(active)

    assert active.status == "active"
    assert active.keywords == "alpha beta gamma"
    assert "untrusted" in (active.proposed_keywords or "")
    assert active.proposal_count == 1


def test_pattern_matching_ignores_proposed_patterns(db, tenant_company_supplier):
    tenant, company, supplier, _user = tenant_company_supplier
    db.add(SupplierPattern(
        tenant_id=tenant.id,
        company_id=company.id,
        supplier_id=supplier.id,
        keywords="example supplier wholesale trading",
        proposed_keywords="example supplier wholesale trading",
        status="proposed",
    ))
    db.commit()

    result = match_supplier_by_active_pattern(db, tenant.id, company.id, "Example Supplier Wholesale Trading")
    assert result is None


def test_pattern_matching_preserves_existing_active_patterns(db, tenant_company_supplier):
    tenant, company, supplier, _user = tenant_company_supplier
    db.add(SupplierPattern(
        tenant_id=tenant.id,
        company_id=company.id,
        supplier_id=supplier.id,
        keywords="example supplier wholesale trading",
        status="active",
    ))
    db.commit()

    result = match_supplier_by_active_pattern(db, tenant.id, company.id, "Example Supplier Wholesale Trading")
    assert result is not None
    assert result.id == supplier.id


def test_manual_review_promotes_pattern_with_provenance(db, tenant_company_supplier):
    tenant, company, supplier, user = tenant_company_supplier
    batch, row = _batch_and_row(db, tenant, company, supplier_name="Example OCR Name")

    db.add(InvoiceRowCorrection(row_id=row.id, batch_id=batch.id, supplier_name=supplier.supplier_name))
    db.flush()
    promoted = promote_supplier_pattern_from_row(
        db,
        batch=batch,
        row=row,
        user=user,
        outcome_source="manual_review",
    )
    db.commit()

    pattern = db.query(SupplierPattern).filter_by(supplier_id=supplier.id).one()
    assert promoted is True
    assert pattern.status == "active"
    assert pattern.keywords
    assert pattern.source_batch_id == batch.id
    assert pattern.source_row_id == row.id
    assert pattern.trusted_outcome_source == "manual_review"
    assert pattern.created_by == user.id
    assert pattern.activated_by == user.id
    assert pattern.last_trusted_at is not None


def test_supplier_pattern_trust_schema_is_declared():
    model_columns = set(SupplierPattern.__table__.columns.keys())
    for column in {
        "status",
        "trusted_outcome_source",
        "source_batch_id",
        "source_row_id",
        "created_by",
        "activated_at",
        "activated_by",
        "last_trusted_at",
        "proposed_keywords",
        "proposal_count",
        "last_proposed_at",
    }:
        assert column in model_columns

    migration = open("alembic/versions/2026_07_07_0005_supplier_pattern_trust.py", encoding="utf-8").read()
    startup = open("app/main.py", encoding="utf-8").read()
    assert 'revision = "20260707_0005"' in migration
    assert "ALTER TABLE supplier_patterns ADD COLUMN IF NOT EXISTS status" in startup

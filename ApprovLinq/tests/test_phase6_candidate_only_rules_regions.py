from __future__ import annotations

import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceRow, Tenant
import app.db.review_models  # registers review/audit tables
from app.db.review_models import CorrectionRule, InvoiceFieldCandidate
from app.routers.batches import _apply_saved_rules
from app.services.invoice_arbitration import arbitrate_invoice_row


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant_company_batch(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="T1", tenant_name="Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="C1", company_name="Company")
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="Phase 6 Test Batch",
        source_filename="phase6.pdf",
        status="processing",
    )
    db.add_all([tenant, company, batch])
    db.flush()
    return tenant, company, batch


def _row(db, batch, supplier_name="Old Supplier"):
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        source_filename="phase6.pdf",
        page_no=1,
        supplier_name=supplier_name,
        invoice_number="INV-1",
        description="Goods",
        total_amount=10.0,
        confidence_score=0.40,
        review_required=True,
        review_fields="supplier_name",
    )
    db.add(row)
    db.flush()
    return row


def test_saved_rule_candidate_mode_does_not_write_row_before_resolver():
    db = _session()
    try:
        tenant, company, batch = _tenant_company_batch(db)
        row = _row(db, batch, supplier_name="Old Supplier")
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="old supplier",
            target_value="New Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {"_field_candidates": []}
        _apply_saved_rules(db, batch, row, candidate_payload=payload)

        assert row.supplier_name == "Old Supplier"
        assert payload["_field_candidates"][0]["field_name"] == "supplier_name"
        assert payload["_field_candidates"][0]["candidate_value"] == "New Supplier Ltd"
        assert payload["_field_candidates"][0]["source_type"] == "rule_supplier_alias"
    finally:
        db.close()


def test_resolver_applies_rule_candidate_and_persists_envelope_fields():
    db = _session()
    try:
        tenant, company, batch = _tenant_company_batch(db)
        row = _row(db, batch, supplier_name="Old Supplier")
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="old supplier",
            target_value="New Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {"_field_candidates": []}
        _apply_saved_rules(db, batch, row, candidate_payload=payload)
        result = arbitrate_invoice_row(db, batch, row, payload)
        db.commit()

        assert row.supplier_name == "New Supplier Ltd"
        assert result.decisions["supplier_name"].applied is True
        candidate = db.execute(
            select(InvoiceFieldCandidate)
            .where(
                InvoiceFieldCandidate.row_id == row.id,
                InvoiceFieldCandidate.field_name == "supplier_name",
                InvoiceFieldCandidate.source_type == "rule_supplier_alias",
            )
        ).scalar_one()
        assert candidate.candidate_status == "candidate"
        assert candidate.validation_status == "valid"
        assert candidate.evidence_ref_type == "rule"
        assert candidate.evidence_ref_id
    finally:
        db.close()


def test_saved_region_payload_candidate_is_resolver_only_and_keeps_region_metadata():
    db = _session()
    try:
        _, _, batch = _tenant_company_batch(db)
        row = _row(db, batch, supplier_name="Weak Supplier")
        payload = {
            "_field_candidates": [{
                "field_name": "supplier_name",
                "candidate_value": "Region Supplier Ltd",
                "source_type": "saved_region_candidate",
                "source_id": "remap_hint:42",
                "confidence": 0.90,
                "reason": "candidate_from_saved_region_replay",
                "region_id": 42,
                "page_no": 2,
                "validation_status": "valid",
                "evidence_ref_type": "remap_hint",
                "evidence_ref_id": "42",
            }],
        }

        assert row.supplier_name == "Weak Supplier"
        arbitrate_invoice_row(db, batch, row, payload)
        db.commit()

        assert row.supplier_name == "Region Supplier Ltd"
        candidate = db.execute(
            select(InvoiceFieldCandidate)
            .where(
                InvoiceFieldCandidate.row_id == row.id,
                InvoiceFieldCandidate.source_type == "saved_region_candidate",
            )
        ).scalar_one()
        assert candidate.region_id == 42
        assert candidate.page_no == 2
        assert candidate.validation_status == "valid"
    finally:
        db.close()

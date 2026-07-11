from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceRow, Tenant
import app.db.review_models  # registers review/audit tables
from app.db.review_models import CorrectionRule


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant_company_batch(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="P7", tenant_name="Phase 7 Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="C7", company_name="Phase 7 Co")
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="Phase 7 Batch",
        source_filename="phase7.pdf",
        status="processing",
    )
    db.add_all([tenant, company, batch])
    db.flush()
    return tenant, company, batch


def test_field_resolver_preserves_arbitration_decision_path():
    from app.routers.batches import _apply_saved_rules
    from app.services.field_resolver import resolve_invoice_row

    db = _session()
    try:
        tenant, company, batch = _tenant_company_batch(db)
        row = InvoiceRow(
            batch_id=batch.id,
            tenant_id=tenant.id,
            company_id=company.id,
            source_filename="phase7.pdf",
            page_no=1,
            supplier_name="Old Supplier",
            invoice_number="INV-7",
            description="Goods",
            total_amount=10.0,
            confidence_score=0.40,
            review_required=True,
            review_fields="supplier_name",
        )
        db.add(row)
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
        result = resolve_invoice_row(db, batch, row, payload)

        assert result.decisions["supplier_name"].applied is True
        assert row.supplier_name == "New Supplier Ltd"
        assert payload["_candidates_arbitrated"] is True
    finally:
        db.close()


def test_provider_gateway_delegates_to_extractor(monkeypatch):
    from app.services import provider_gateway

    calls = []

    def fake_process(pdf_path, *, page_index, scan_mode, openai_api_key, account_company_name):
        calls.append((pdf_path, page_index, scan_mode, openai_api_key, account_company_name))
        return [{"page_no": page_index + 1, "supplier_name": "Supplier"}]

    monkeypatch.setattr(provider_gateway, "process_pdf_page_rows", fake_process)

    rows = provider_gateway.process_page_rows_with_timeout(
        "invoice.pdf",
        page_index=2,
        scan_mode="summary",
        openai_api_key="key",
        account_company_name="Company",
    )

    assert rows == [{"page_no": 3, "supplier_name": "Supplier"}]
    assert calls == [("invoice.pdf", 2, "summary", "key", "Company")]


def test_process_endpoint_queues_scan_orchestrator(monkeypatch):
    from app.routers import batches

    batch = type("Batch", (), {"id": uuid.uuid4(), "status": "created", "notes": ""})()

    class FakeDb:
        committed = False

        def commit(self):
            self.committed = True

    class FakeBackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn, *args):
            self.tasks.append((fn, args))

    monkeypatch.setattr(batches, "_get_batch_for_tenant", lambda db, batch_id, tenant_id: batch)
    monkeypatch.setattr(batches, "_set_active", lambda batch_id: True)

    background_tasks = FakeBackgroundTasks()
    db = FakeDb()
    result = batches.process_batch(batch.id, background_tasks, db=db, tenant_id=uuid.uuid4(), _user=object())

    assert result == {"ok": True, "status": "processing"}
    assert batch.status == "processing"
    assert db.committed is True
    queued_fn, queued_args = background_tasks.tasks[0]
    assert queued_fn.__module__ == "app.services.scan_orchestrator"
    assert queued_fn.__name__ == "process_batch_job"
    assert queued_args[0] == batch.id


def test_duplicate_resolver_delegates_without_changing_contract(monkeypatch):
    from app.services import duplicate_resolver

    calls = []
    batch = type("Batch", (), {"id": uuid.uuid4()})()

    def fake_within(db, batch_id, scan_run_id=None):
        calls.append(("within", batch_id, scan_run_id))
        return 2

    def fake_prior(db, batch_arg, scan_run_id=None):
        calls.append(("prior", batch_arg, scan_run_id))
        return 3

    import app.routers.batches as batches

    monkeypatch.setattr(batches, "_mark_duplicate_invoice_rows", fake_within)
    monkeypatch.setattr(duplicate_resolver, "detect_cross_batch_duplicates", fake_prior)

    assert duplicate_resolver.detect_within_batch_duplicates(object(), batch.id, "run-1") == 2
    assert duplicate_resolver.detect_prior_batch_duplicates(object(), batch, "run-1") == 3
    assert calls == [("within", batch.id, "run-1"), ("prior", batch, "run-1")]

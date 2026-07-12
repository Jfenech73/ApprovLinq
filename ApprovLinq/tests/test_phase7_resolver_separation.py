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


def test_supplier_alias_rule_overrides_strong_raw_supplier_after_resolver_split():
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
            supplier_name="Wrong Supplier",
            invoice_number="INV-STRONG",
            description="Goods",
            net_amount=10.0,
            vat_amount=1.8,
            total_amount=11.8,
            confidence_score=0.95,
            review_required=False,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Correct Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [],
            "supplier_name": "Wrong Supplier",
            "invoice_number": "INV-STRONG",
            "description": "Goods",
            "net_amount": 10.0,
            "vat_amount": 1.8,
            "total_amount": 11.8,
            "confidence_score": 0.95,
        }
        _apply_saved_rules(db, batch, row, candidate_payload=payload)
        result = resolve_invoice_row(db, batch, row, payload)

        decision = result.decisions["supplier_name"]
        assert decision.winning_candidate is not None
        assert decision.winning_candidate.source_type == "rule_supplier_alias"
        assert decision.applied is True
        assert decision.conflict is False
        assert row.supplier_name == "Correct Supplier Ltd"
    finally:
        db.close()


def test_supplier_alias_rule_overrides_competing_supplier_master_candidate():
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
            supplier_name="Wrong Supplier",
            invoice_number="INV-MASTER",
            net_amount=10.0,
            vat_amount=1.8,
            total_amount=11.8,
            confidence_score=0.95,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Correct Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [{
                "field_name": "supplier_name",
                "candidate_value": "Wrong Supplier Master",
                "source_type": "supplier_master",
                "confidence": 0.96,
                "reason": "competing supplier master candidate",
                "validation_status": "valid",
                "candidate_status": "candidate",
            }],
            "supplier_name": "Wrong Supplier",
            "invoice_number": "INV-MASTER",
            "net_amount": 10.0,
            "vat_amount": 1.8,
            "total_amount": 11.8,
            "confidence_score": 0.95,
        }
        _apply_saved_rules(db, batch, row, candidate_payload=payload)
        result = resolve_invoice_row(db, batch, row, payload)

        decision = result.decisions["supplier_name"]
        assert decision.winning_candidate is not None
        assert decision.winning_candidate.source_type == "rule_supplier_alias"
        assert decision.applied is True
        assert decision.conflict is False
        assert row.supplier_name == "Correct Supplier Ltd"
    finally:
        db.close()


def test_newest_matching_supplier_alias_rule_wins_deterministically():
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
            supplier_name="Wrong Supplier",
            invoice_number="INV-NEWEST",
            net_amount=10.0,
            vat_amount=1.8,
            total_amount=11.8,
            confidence_score=0.95,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Old Target Ltd",
            active=True,
        ))
        db.flush()
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Amended Target Ltd",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [],
            "supplier_name": "Wrong Supplier",
            "invoice_number": "INV-NEWEST",
            "net_amount": 10.0,
            "vat_amount": 1.8,
            "total_amount": 11.8,
            "confidence_score": 0.95,
        }
        _apply_saved_rules(db, batch, row, candidate_payload=payload)

        assert [c["candidate_value"] for c in payload["_field_candidates"] if c["source_type"] == "rule_supplier_alias"] == [
            "Old Target Ltd",
            "Amended Target Ltd",
        ]

        result = resolve_invoice_row(db, batch, row, payload)

        decision = result.decisions["supplier_name"]
        assert decision.winning_candidate is not None
        assert decision.winning_candidate.value == "Amended Target Ltd"
        assert decision.applied is True
        assert decision.conflict is False
        assert row.supplier_name == "Amended Target Ltd"
    finally:
        db.close()


def test_text_correction_rule_can_write_light_field_through_resolver():
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
            supplier_name="Known Supplier",
            invoice_number="BAD-READ",
            net_amount=10.0,
            vat_amount=1.8,
            total_amount=11.8,
            confidence_score=0.95,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="text_correction",
            field_name="invoice_number",
            source_pattern="BAD-READ",
            target_value="INV-100",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [],
            "supplier_name": "Known Supplier",
            "invoice_number": "BAD-READ",
            "net_amount": 10.0,
            "vat_amount": 1.8,
            "total_amount": 11.8,
            "confidence_score": 0.95,
        }
        _apply_saved_rules(db, batch, row, candidate_payload=payload)
        result = resolve_invoice_row(db, batch, row, payload)

        decision = result.decisions["invoice_number"]
        assert decision.winning_candidate is not None
        assert decision.winning_candidate.source_type == "rule_text_correction"
        assert decision.applied is True
        assert row.invoice_number == "INV-100"
    finally:
        db.close()


def test_supplier_alias_rule_matches_original_supplier_after_master_enrichment_changes_payload():
    from app.routers.batches import _apply_saved_rules

    db = _session()
    try:
        tenant, company, batch = _tenant_company_batch(db)
        row = InvoiceRow(
            batch_id=batch.id,
            tenant_id=tenant.id,
            company_id=company.id,
            source_filename="phase7.pdf",
            page_no=1,
            supplier_name="Wrong Supplier Master",
            invoice_number="INV-ORIG",
            total_amount=11.8,
            confidence_score=0.95,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Correct Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [],
            "_original_field_values": {"supplier_name": "Wrong Supplier"},
            "supplier_name": "Wrong Supplier Master",
        }
        _apply_saved_rules(db, batch, row, candidate_payload=payload)

        assert payload["_field_candidates"]
        assert payload["_field_candidates"][0]["source_type"] == "rule_supplier_alias"
        assert payload["_field_candidates"][0]["candidate_value"] == "Correct Supplier Ltd"
        assert payload["_field_candidates"][0]["evidence"] == "Wrong Supplier"
    finally:
        db.close()


def test_provider_baseline_resolves_explicit_rule_candidate_before_payload_sync():
    from app.routers.batches import _has_new_provider_baseline_rule_candidate
    from app.services.field_resolver import resolve_invoice_row
    from app.services.saved_region_service import apply_saved_rule_candidates

    db = _session()
    try:
        tenant, company, batch = _tenant_company_batch(db)
        row = InvoiceRow(
            batch_id=batch.id,
            tenant_id=tenant.id,
            company_id=company.id,
            source_filename="phase7.pdf",
            page_no=1,
            supplier_name="Wrong Supplier",
            invoice_number="INV-BASELINE",
            description="Goods",
            net_amount=10.0,
            vat_amount=1.8,
            total_amount=11.8,
            confidence_score=0.95,
            review_required=False,
        )
        db.add(row)
        db.add(CorrectionRule(
            tenant_id=tenant.id,
            company_id=company.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="wrong supplier",
            target_value="Correct Supplier Ltd",
            active=True,
        ))
        db.commit()

        payload = {
            "_field_candidates": [],
            "supplier_name": "Wrong Supplier",
            "invoice_number": "INV-BASELINE",
            "description": "Goods",
            "net_amount": 10.0,
            "vat_amount": 1.8,
            "total_amount": 11.8,
            "confidence_score": 0.95,
        }
        baseline_before_rules = {
            "supplier_name": row.supplier_name,
            "supplier_posting_account": row.supplier_posting_account,
            "nominal_account_code": row.nominal_account_code,
            "invoice_number": row.invoice_number,
            "invoice_date": row.invoice_date,
            "description": row.description,
            "net_amount": row.net_amount,
            "vat_amount": row.vat_amount,
            "total_amount": row.total_amount,
        }

        candidate_start = len(payload["_field_candidates"])
        apply_saved_rule_candidates(db, batch, row, candidate_payload=payload)
        assert _has_new_provider_baseline_rule_candidate(payload, candidate_start) is True

        result = resolve_invoice_row(db, batch, row, payload)
        baseline_changed = [
            field for field, before_value in baseline_before_rules.items()
            if str(getattr(row, field, None) or "").strip() != str(before_value or "").strip()
        ]
        for field in baseline_changed:
            payload[field] = getattr(row, field, None)

        assert result.decisions["supplier_name"].applied is True
        assert row.supplier_name == "Correct Supplier Ltd"
        assert payload["supplier_name"] == "Correct Supplier Ltd"
        assert payload["_candidates_arbitrated"] is True
        assert "rule_candidate:supplier_alias" in (row.method_used or "")
        assert "arbitrated:rule_supplier_alias:supplier_name" in (row.method_used or "")
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

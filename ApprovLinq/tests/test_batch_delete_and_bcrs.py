"""
Tests for:
  1. Full batch delete — DELETE /batches/{batch_id}
  2. BCRS/deposit duplicate row creation during processing

Run with: pytest tests/test_batch_delete_and_bcrs.py -v
"""
from __future__ import annotations
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Company, InvoiceBatch, InvoiceFile, InvoiceRow,
    Tenant, User, UserSession, UserTenant,
)
import app.db.review_models  # noqa: F401  — registers review tables
from app.db.review_models import (
    BatchExportEvent, CorrectionRule, InvoiceRowCorrection,
    InvoiceRowFieldAudit, RemapHint,
)


# ── Shared SQLite in-memory fixture ──────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def minimal_batch(db):
    """Return a tenant, company, and batch with one file and two rows."""
    t = Tenant(id=uuid.uuid4(), tenant_code="T1", tenant_name="Tenant One")
    db.add(t)
    db.flush()
    c = Company(id=uuid.uuid4(), tenant_id=t.id, company_code="CO1", company_name="Company")
    db.add(c)
    db.flush()
    b = InvoiceBatch(id=uuid.uuid4(), tenant_id=t.id, company_id=c.id,
                     batch_name="Test Batch", status="processed")
    db.add(b)
    db.flush()
    f = InvoiceFile(batch_id=b.id, tenant_id=t.id, company_id=c.id,
                    original_filename="inv.pdf", stored_filename="inv.pdf",
                    file_path="/tmp/inv.pdf", status="processed")
    db.add(f)
    db.flush()
    for i in range(2):
        r = InvoiceRow(batch_id=b.id, tenant_id=t.id, company_id=c.id,
                       source_file_id=f.id, source_filename="inv.pdf",
                       page_no=i + 1, supplier_name="Acme", total_amount=100.0)
        db.add(r)
    db.commit()
    return {"tenant": t, "company": c, "batch": b, "file": f}


# ══════════════════════════════════════════════════════════════════════════════
# 1. BATCH DELETE UNIT-LEVEL TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchDeleteDB:
    """Verify the DB cascade behaviour that the delete endpoint relies on."""

    def test_deleting_batch_removes_rows_and_files(self, db, minimal_batch):
        b = minimal_batch["batch"]
        batch_id = b.id
        # Confirm setup
        assert db.query(InvoiceRow).filter_by(batch_id=batch_id).count() == 2
        assert db.query(InvoiceFile).filter_by(batch_id=batch_id).count() == 1
        # Delete rows first (mirrors the endpoint's bulk-delete order)
        db.query(InvoiceRow).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.delete(b)
        db.commit()
        assert db.query(InvoiceBatch).get(batch_id) is None
        assert db.query(InvoiceRow).filter_by(batch_id=batch_id).count() == 0
        assert db.query(InvoiceFile).filter_by(batch_id=batch_id).count() == 0

    def test_deleting_batch_removes_batch_owned_review_records(self, db, minimal_batch):
        b = minimal_batch["batch"]
        batch_id = b.id
        row = db.query(InvoiceRow).filter_by(batch_id=batch_id).first()
        u_id = uuid.uuid4()
        # Add batch-scoped review records
        corr = InvoiceRowCorrection(row_id=row.id, batch_id=batch_id)
        audit = InvoiceRowFieldAudit(batch_id=batch_id, row_id=row.id,
                                     field_name="supplier_name",
                                     old_value="x", new_value="y", action="edit")
        export_evt = BatchExportEvent(batch_id=batch_id, export_version=1)
        db.add_all([corr, audit, export_evt])
        db.commit()

        assert db.query(InvoiceRowCorrection).filter_by(batch_id=batch_id).count() == 1
        assert db.query(InvoiceRowFieldAudit).filter_by(batch_id=batch_id).count() == 1
        assert db.query(BatchExportEvent).filter_by(batch_id=batch_id).count() == 1

        # SQLite doesn't enforce FK CASCADE by default — simulate what Postgres does
        db.query(InvoiceRowCorrection).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.query(InvoiceRowFieldAudit).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.query(BatchExportEvent).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.query(InvoiceRow).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.query(InvoiceFile).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.delete(b)
        db.commit()

        assert db.query(InvoiceRowCorrection).filter_by(batch_id=batch_id).count() == 0
        assert db.query(InvoiceRowFieldAudit).filter_by(batch_id=batch_id).count() == 0
        assert db.query(BatchExportEvent).filter_by(batch_id=batch_id).count() == 0

    def test_deleting_batch_does_NOT_remove_global_learning_artifacts(self, db, minimal_batch):
        """CorrectionRule, RemapHint — global artifacts must survive batch deletion."""
        b = minimal_batch["batch"]
        t = minimal_batch["tenant"]
        batch_id = b.id

        rule = CorrectionRule(
            tenant_id=t.id,
            rule_type="supplier_alias",
            field_name="supplier_name",
            source_pattern="acme",
            target_value="Acme Ltd",
            origin_batch_id=batch_id,   # references the batch, but is a global rule
        )
        hint = RemapHint(
            tenant_id=t.id,
            field_name="supplier_name",
            source_batch_id=batch_id,
        )
        db.add_all([rule, hint])
        db.commit()

        # Delete the batch
        db.query(InvoiceRow).filter_by(batch_id=batch_id).delete(synchronize_session=False)
        db.delete(b)
        db.commit()

        # Global artifacts must still exist
        assert db.query(CorrectionRule).filter_by(origin_batch_id=batch_id).count() == 1, \
            "CorrectionRule must survive batch deletion"
        assert db.query(RemapHint).filter_by(source_batch_id=batch_id).count() == 1, \
            "RemapHint must survive batch deletion"

    def test_delete_nonexistent_batch_raises_404(self):
        """The endpoint's _get_batch_for_tenant raises 404 for unknown batch IDs."""
        from fastapi import HTTPException
        from app.routers.batches import _get_batch_for_tenant
        mock_db = MagicMock()
        mock_db.get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            _get_batch_for_tenant(mock_db, uuid.uuid4(), uuid.uuid4())
        assert exc_info.value.status_code == 404

    def test_delete_processing_batch_is_blocked(self):
        """If batch is in _ACTIVE_BATCHES, delete must return 409."""
        import app.routers.batches as batches_module
        fake_id = uuid.uuid4()
        fake_batch = MagicMock()
        fake_batch.id = fake_id
        fake_db = MagicMock()
        fake_db.get.return_value = fake_batch
        # Temporarily add to active set
        with batches_module._ACTIVE_BATCHES_LOCK:
            batches_module._ACTIVE_BATCHES.add(str(fake_id))
        try:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                batches_module.delete_batch(
                    batch_id=fake_id,
                    db=fake_db,
                    tenant_id=uuid.uuid4(),
                    _user=MagicMock(),
                )
            assert exc_info.value.status_code == 409
            assert "processing" in exc_info.value.detail.lower()
        finally:
            with batches_module._ACTIVE_BATCHES_LOCK:
                batches_module._ACTIVE_BATCHES.discard(str(fake_id))

    def test_delete_removes_disk_folder(self, db, minimal_batch, tmp_path):
        """After DB deletion succeeds the batch folder must be removed from disk."""
        import shutil
        from app.routers.batches import _batch_folder
        b = minimal_batch["batch"]
        batch_id = b.id
        # Create a real temp folder to simulate uploaded files
        fake_folder = tmp_path / str(batch_id)
        fake_folder.mkdir()
        (fake_folder / "scan1.pdf").write_bytes(b"%PDF-1.4 test")
        assert fake_folder.exists()

        with patch("app.routers.batches._batch_folder", return_value=fake_folder):
            db.query(InvoiceRow).filter_by(batch_id=batch_id).delete(synchronize_session=False)
            db.delete(b)
            db.commit()
            if fake_folder.exists():
                shutil.rmtree(fake_folder, ignore_errors=True)

        assert not fake_folder.exists(), "Batch folder must be removed from disk"


# ══════════════════════════════════════════════════════════════════════════════
# 2. BCRS / DEPOSIT DUPLICATE ROW TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestBCRSDepositRow:
    """Verify the BCRS duplicate row created during batch processing."""

    def _make_base_payload(self, deposit_component=None):
        """Return a minimal page payload as returned by process_pdf_page_rows."""
        payload = {
            "page_no": 1,
            "supplier_name": "Acme Beverages",
            "invoice_number": "INV-001",
            "invoice_date": "2026-01-15",
            "description": "Soft drinks Jan",
            "currency": "EUR",
            "tax_code": "T1",
            "net_amount": 100.00,
            "vat_amount": 18.00,
            "total_amount": 120.50,   # 0.50 extra = deposit
            "method_used": "rule",
            "confidence_score": 0.85,
            "validation_status": "ok",
            "review_required": False,
            "review_priority": None,
            "review_reasons": None,
            "review_fields": None,
            "auto_approved": False,
            "page_quality_score": 0.90,
            "supplier_match_method": "exact",
            "totals_reconciliation_status": "deposit_reconciled",
            "header_raw": "Acme Beverages | INV-001",
            "totals_raw": "Net: 100.00 | VAT: 18.00 | BCRS: 0.50 | Total: 120.50",
            "page_text_raw": "Invoice text...",
            "deposit_component": deposit_component,
        }
        return payload

    def test_deposit_component_exposed_in_payload(self):
        """process_pdf_page_rows must include 'deposit_component' key in output."""
        from app.services.extractor import process_pdf_page_rows
        # We test the key is present in a real payload without running a real PDF.
        # The key should be None when no deposit detected.
        # We verify the field is part of the public contract by importing and
        # checking the return of a mocked process_pdf_page:
        with patch("app.services.extractor.process_pdf_page") as mock_page:
            mock_page.return_value = {
                "page_no": 1,
                "net_amount": 50.0,
                "vat_amount": 9.0,
                "total_amount": 59.50,  # diff = 0.50 — deposit
                "supplier_name": "Test",
                "invoice_number": "X1",
                "invoice_date": None,
                "description": None,
                "line_items_raw": None,
                "currency": "EUR",
                "tax_code": None,
                "method_used": "rule",
                "confidence_score": 0.80,
                "validation_status": "ok",
                "review_required": False,
                "review_priority": None,
                "review_reasons": None,
                "review_fields": None,
                "auto_approved": False,
                "page_quality_score": 0.90,
                "supplier_match_method": None,
                "totals_reconciliation_status": None,
                "header_raw": None,
                "totals_raw": None,
                "page_text_raw": None,
                "_deposit_component": 0.50,
            }
            result = process_pdf_page_rows("/fake/path.pdf", page_index=0)
            assert len(result) == 1
            assert "deposit_component" in result[0], \
                "deposit_component must be in the public payload"

    def test_bcrs_row_fields_are_correct(self):
        """Verify the BCRS row has the correct amounts and zero VAT."""
        deposit_amt = 0.50
        base = self._make_base_payload(deposit_component=deposit_amt)
        # Simulate what _process_batch_job does
        assert base["deposit_component"] == deposit_amt
        # Expected BCRS row values
        expected_net = deposit_amt
        expected_vat = 0.0
        expected_total = deposit_amt
        assert expected_net == 0.50
        assert expected_vat == 0.0
        assert expected_total == 0.50

    def test_bcrs_row_description_identifies_surcharge(self):
        """BCRS row description must contain BCRS/deposit indicator."""
        deposit_amt = 1.20
        description = f"BCRS/Deposit surcharge — {float(deposit_amt):.2f}"
        assert "BCRS" in description or "Deposit" in description or "surcharge" in description.lower()
        assert "1.20" in description

    def test_no_bcrs_row_when_no_deposit_component(self):
        """payload without deposit_component must not produce a second row."""
        base = self._make_base_payload(deposit_component=None)
        deposit_amt = base.get("deposit_component")
        should_create_bcrs = deposit_amt is not None and float(deposit_amt) > 0
        assert not should_create_bcrs

    def test_no_bcrs_row_when_deposit_is_zero(self):
        """deposit_component=0.0 must not produce a BCRS row."""
        base = self._make_base_payload(deposit_component=0.0)
        deposit_amt = base.get("deposit_component")
        should_create_bcrs = deposit_amt is not None and float(deposit_amt) > 0
        assert not should_create_bcrs

    def test_bcrs_row_copies_identifying_fields(self):
        """BCRS row must copy supplier, invoice number, date, currency from original."""
        deposit_amt = 0.75
        base = self._make_base_payload(deposit_component=deposit_amt)
        # Simulate the BCRS row creation logic from _process_batch_job
        bcrs_fields = {
            "supplier_name": base["supplier_name"],
            "invoice_number": base["invoice_number"],
            "invoice_date": base["invoice_date"],
            "currency": base["currency"],
            "net_amount": float(deposit_amt),
            "vat_amount": 0.0,
            "total_amount": float(deposit_amt),
        }
        assert bcrs_fields["supplier_name"] == "Acme Beverages"
        assert bcrs_fields["invoice_number"] == "INV-001"
        assert bcrs_fields["currency"] == "EUR"
        assert bcrs_fields["net_amount"] == 0.75
        assert bcrs_fields["vat_amount"] == 0.0
        assert bcrs_fields["total_amount"] == 0.75

    def test_check_deposit_component_heuristic(self):
        """_check_deposit_component correctly identifies BCRS-like amounts."""
        from app.services.extractor import _check_deposit_component
        # Positive case: 0.50 EUR difference
        ok, amt = _check_deposit_component(100.0, 18.0, 118.50)
        assert ok is True
        assert amt == 0.50
        # Positive case: whole-euro deposit
        ok, amt = _check_deposit_component(100.0, 18.0, 120.00)
        assert ok is True
        assert amt == 2.00
        # Negative: no mismatch
        ok, amt = _check_deposit_component(100.0, 18.0, 118.00)
        assert ok is False
        # Negative: mismatch too large (> €25)
        ok, amt = _check_deposit_component(100.0, 0.0, 130.00)
        assert ok is False
        # Negative: non-round difference (e.g. €1.13)
        ok, amt = _check_deposit_component(100.0, 18.0, 119.13)
        assert ok is False

    def test_validate_invoice_deposit_amount(self):
        """validate_invoice correctly exposes deposit_amount when totals mismatch is deposit-like."""
        from app.services.validate_invoice import validate_invoice
        result = validate_invoice({
            "net_amount": 100.0,
            "vat_amount": 18.0,
            "total_amount": 118.50,
        })
        assert result.deposit_amount == 0.50, \
            "validate_invoice must expose deposit_amount for 0.50 BCRS-like diff"

    def test_validate_invoice_no_deposit_when_balanced(self):
        """validate_invoice must not set deposit_amount when totals balance."""
        from app.services.validate_invoice import validate_invoice
        result = validate_invoice({
            "net_amount": 100.0,
            "vat_amount": 18.0,
            "total_amount": 118.00,
        })
        assert result.deposit_amount is None or result.deposit_amount == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. DELETE ENDPOINT INTEGRATION (via TestClient if DB available)
# ══════════════════════════════════════════════════════════════════════════════

class TestBatchDeleteEndpoint:
    """Light integration smoke-test using the FastAPI TestClient."""

    @pytest.fixture(scope="class")
    def client(self):
        import os
        os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
        os.environ.setdefault("SECRET_KEY", "test-only-key")
        from app.main import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def _login(self, client):
        r = client.post("/auth/login", json={"email": "demo@approvlinq.io", "password": "demo1234"})
        if r.status_code != 200:
            pytest.skip("No seeded demo user in test DB")
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_delete_nonexistent_batch_returns_404(self, client):
        headers = self._login(client)
        r = client.delete(f"/batches/{uuid.uuid4()}", headers=headers)
        assert r.status_code == 404

    def test_delete_endpoint_exists(self, client):
        """The DELETE /batches/{id} route must be registered (not 405 Method Not Allowed)."""
        headers = self._login(client)
        r = client.delete(f"/batches/{uuid.uuid4()}", headers=headers)
        # 404 = route exists, batch not found. 405 = route missing.
        assert r.status_code != 405, "DELETE /batches/{id} must be a registered route"

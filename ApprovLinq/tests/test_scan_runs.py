from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceReadHeader, InvoiceRow, ScanRun, Tenant
from app.db.review_models import InvoiceFieldCandidate, InvoiceRowCorrection
from app.services.candidate_outcomes import label_batch_candidates
from app.services.corrected_exporter import build_corrected_rows
from app.services.scan_runs import create_scan_run, mark_scan_run_completed


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Company.__table__,
            InvoiceBatch.__table__,
            ScanRun.__table__,
            InvoiceRow.__table__,
            InvoiceReadHeader.__table__,
            InvoiceFieldCandidate.__table__,
            InvoiceRowCorrection.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def _batch(db):
    tenant = Tenant(tenant_code="tenant", tenant_name="Tenant")
    db.add(tenant)
    db.flush()
    company = Company(tenant_id=tenant.id, company_code="co", company_name="Company")
    db.add(company)
    db.flush()
    batch = InvoiceBatch(tenant_id=tenant.id, company_id=company.id, batch_name="Batch", scan_mode="summary")
    db.add(batch)
    db.commit()
    return batch


def _row(db, batch: InvoiceBatch, run: ScanRun, invoice_number: str, total: float) -> InvoiceRow:
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        scan_run_id=run.id,
        page_no=1,
        supplier_name="Supplier Ltd",
        invoice_number=invoice_number,
        total_amount=total,
        currency="EUR",
        review_required=False,
    )
    db.add(row)
    db.flush()
    return row


def test_reprocess_creates_new_current_run_without_deleting_prior_rows():
    db = _db()
    batch = _batch(db)

    run1 = create_scan_run(db, batch)
    row1 = _row(db, batch, run1, "INV-1", 10.0)
    mark_scan_run_completed(db, run1, status="processed", page_count=1, row_count=1, notes="first")
    db.commit()

    run2 = create_scan_run(db, batch)
    row2 = _row(db, batch, run2, "INV-2", 20.0)
    mark_scan_run_completed(db, run2, status="processed", page_count=1, row_count=1, notes="second")
    db.commit()

    rows = db.execute(select(InvoiceRow).where(InvoiceRow.batch_id == batch.id)).scalars().all()
    assert {r.id for r in rows} == {row1.id, row2.id}
    assert batch.current_scan_run_id == run2.id
    assert run2.parent_run_id == run1.id
    assert [r["invoice_number"] for r in build_corrected_rows(db, batch)] == ["INV-2"]


def test_candidates_and_corrections_are_preserved_but_batch_labelling_uses_current_run():
    db = _db()
    batch = _batch(db)

    run1 = create_scan_run(db, batch)
    old_row = _row(db, batch, run1, "OLD", 10.0)
    db.add(InvoiceFieldCandidate(
        id=1,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        batch_id=batch.id,
        scan_run_id=run1.id,
        row_id=old_row.id,
        field_name="invoice_number",
        candidate_value="OLD",
        normalised_value="old",
        source_type="azure_di",
        selected=True,
        applied=True,
    ))
    db.add(InvoiceRowCorrection(row_id=old_row.id, batch_id=batch.id, scan_run_id=run1.id, row_reviewed=True))
    db.commit()

    run2 = create_scan_run(db, batch)
    current_row = _row(db, batch, run2, "NEW", 20.0)
    db.add(InvoiceFieldCandidate(
        id=2,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        batch_id=batch.id,
        scan_run_id=run2.id,
        row_id=current_row.id,
        field_name="invoice_number",
        candidate_value="NEW",
        normalised_value="new",
        source_type="azure_di",
        selected=True,
        applied=True,
    ))
    db.commit()

    labelled = label_batch_candidates(db, batch=batch, outcome_source="export")
    candidates = db.execute(select(InvoiceFieldCandidate)).scalars().all()

    assert labelled == 1
    assert len(candidates) == 2
    assert {c.scan_run_id for c in candidates} == {run1.id, run2.id}
    assert [c.final_value for c in candidates if c.scan_run_id == run1.id] == [None]
    assert [c.final_value for c in candidates if c.scan_run_id == run2.id] == ["NEW"]


def test_scan_run_schema_tokens_are_declared():
    model_columns = set(ScanRun.__table__.columns.keys())
    assert {
        "batch_id",
        "run_number",
        "parent_run_id",
        "settings_fingerprint",
        "provider_config_fingerprint",
        "selected_backend",
    } <= model_columns
    assert "current_scan_run_id" in InvoiceBatch.__table__.columns
    assert "scan_run_id" in InvoiceRow.__table__.columns
    assert "scan_run_id" in InvoiceFieldCandidate.__table__.columns

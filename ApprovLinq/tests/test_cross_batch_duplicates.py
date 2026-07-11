from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Company,
    INVOICE_ROW_STATUS_ACTIVE,
    InvoiceBatch,
    InvoiceReadHeader,
    InvoiceRow,
    ScanRun,
    Tenant,
    User,
)
from app.db.review_models import (
    InvoiceDuplicateCandidate,
    InvoiceFieldCandidate,
    InvoiceRowCorrection,
    InvoiceRowFieldAudit,
)
from app.routers.review import get_review_workspace, restore_review_row
from app.services.corrected_exporter import build_corrected_rows
from app.services.cross_batch_duplicates import detect_cross_batch_duplicates
from app.services.scan_runs import create_scan_run, mark_scan_run_completed


ROOT = Path(__file__).resolve().parents[1]


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            Company.__table__,
            User.__table__,
            InvoiceBatch.__table__,
            ScanRun.__table__,
            InvoiceRow.__table__,
            InvoiceReadHeader.__table__,
            InvoiceDuplicateCandidate.__table__,
            InvoiceFieldCandidate.__table__,
            InvoiceRowCorrection.__table__,
            InvoiceRowFieldAudit.__table__,
        ],
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE invoice_row_field_audits")
        conn.exec_driver_sql(
            """
            CREATE TABLE invoice_row_field_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id CHAR(32) NOT NULL,
                scan_run_id CHAR(32),
                row_id BIGINT NOT NULL,
                field_name VARCHAR(80) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                action VARCHAR(40) NOT NULL,
                note TEXT,
                rule_created BOOLEAN NOT NULL DEFAULT 0,
                force_added BOOLEAN NOT NULL DEFAULT 0,
                user_id CHAR(32),
                username VARCHAR(255),
                created_at DATETIME
            )
            """
        )
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def _tenant_company(db, code: str = "tenant"):
    tenant = Tenant(tenant_code=code, tenant_name=f"Tenant {code}")
    db.add(tenant)
    db.flush()
    company = Company(tenant_id=tenant.id, company_code=f"{code}-co", company_name=f"Company {code}")
    user = User(email=f"{code}@example.test", full_name="Reviewer", password_hash="x")
    db.add_all([company, user])
    db.flush()
    return tenant, company, user


def _batch(db, tenant: Tenant, company: Company, name: str, status: str = "processed"):
    batch = InvoiceBatch(
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name=name,
        scan_mode="summary",
        status=status,
    )
    db.add(batch)
    db.commit()
    run = create_scan_run(db, batch)
    db.commit()
    return batch, run


def _row(
    db,
    batch: InvoiceBatch,
    run: ScanRun,
    invoice_number: str,
    total: float = 123.45,
    supplier: str = "Supplier Ltd",
    supplier_vat: str | None = "MT12345678",
    invoice_date: date | None = date(2026, 7, 1),
    currency: str = "EUR",
) -> InvoiceRow:
    row = InvoiceRow(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        scan_run_id=run.id,
        page_no=1,
        supplier_name=supplier,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        total_amount=total,
        currency=currency,
        review_required=False,
    )
    db.add(row)
    db.flush()
    db.add(InvoiceReadHeader(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        scan_run_id=run.id,
        row_id=row.id,
        page_no=1,
        provider_name="test",
        document_type="invoice",
        supplier_name=supplier,
        supplier_vat=supplier_vat,
        invoice_number=invoice_number,
        invoice_date=invoice_date.isoformat() if invoice_date else None,
        total_amount=total,
        currency=currency,
    ))
    db.flush()
    return row


def _exported_prior_row(db):
    tenant, company, user = _tenant_company(db)
    prior_batch, prior_run = _batch(db, tenant, company, "Prior exported", status="processed")
    prior_row = _row(db, prior_batch, prior_run, "INV-100")
    mark_scan_run_completed(db, prior_run, status="processed", page_count=1, row_count=1, notes="prior")
    prior_batch.status = "exported"
    db.commit()
    current_batch, current_run = _batch(db, tenant, company, "Current", status="processed")
    return tenant, company, user, prior_batch, prior_run, prior_row, current_batch, current_run


def test_strong_cross_batch_duplicate_blocks_export_and_preserves_evidence():
    db = _db()
    _tenant, _company, _user, _prior_batch, _prior_run, prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100")
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 1
    row = db.get(InvoiceRow, current_row.id)
    assert row.row_status == "blocked_duplicate"
    assert row.row_status_reason == "cross_batch_duplicate"
    assert "Duplicate of batch 'Prior exported'" in row.row_status_note
    assert str(_prior_batch.id) in row.row_status_note
    assert f"row {prior_row.id}" in row.row_status_note
    assert row.review_required is True
    assert "possible_cross_batch_duplicate" in row.review_reasons
    assert build_corrected_rows(db, current_batch) == []

    candidate = db.execute(select(InvoiceDuplicateCandidate)).scalar_one()
    assert candidate.row_id == current_row.id
    assert candidate.candidate_row_id == prior_row.id
    assert candidate.match_status == "blocked_duplicate"
    assert float(candidate.confidence) >= 0.9
    assert "candidate_batch_name" in candidate.evidence_json

    actions = [a.action for a in db.execute(select(InvoiceRowFieldAudit)).scalars()]
    assert "cross_batch_duplicate_block" in actions


def test_ambiguous_cross_batch_duplicate_is_review_only_and_still_exportable():
    db = _db()
    _tenant, _company, _user, _prior_batch, _prior_run, _prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100", supplier="Different Supplier", supplier_vat="MT87654321")
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 1
    row = db.get(InvoiceRow, current_row.id)
    assert row.row_status == INVOICE_ROW_STATUS_ACTIVE
    assert row.review_required is True
    assert row.validation_status == "review_cross_batch_duplicate"
    assert [r["invoice_number"] for r in build_corrected_rows(db, current_batch)] == ["INV-100"]
    candidate = db.execute(select(InvoiceDuplicateCandidate)).scalar_one()
    assert candidate.match_status == "review_only"
    assert float(candidate.confidence) < 0.9


def test_strong_cross_batch_duplicate_blocks_when_currency_is_missing():
    db = _db()
    _tenant, _company, _user, _prior_batch, _prior_run, _prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100", currency="")
    prior_row = db.execute(
        select(InvoiceRow).where(InvoiceRow.batch_id != current_batch.id)
    ).scalar_one()
    prior_row.currency = None
    headers = db.execute(select(InvoiceReadHeader)).scalars().all()
    for header in headers:
        header.currency = None
        header.CurrencyCode = None
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 1
    row = db.get(InvoiceRow, current_row.id)
    assert row.row_status == "blocked_duplicate"
    candidate = db.execute(select(InvoiceDuplicateCandidate)).scalar_one()
    assert candidate.match_status == "blocked_duplicate"
    assert '"currency_compatible": true' in candidate.evidence_json


def test_strong_cross_batch_duplicate_blocks_when_date_is_missing():
    db = _db()
    _tenant, _company, _user, _prior_batch, _prior_run, _prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100", invoice_date=None)
    prior_row = db.execute(
        select(InvoiceRow).where(InvoiceRow.batch_id != current_batch.id)
    ).scalar_one()
    prior_row.invoice_date = None
    headers = db.execute(select(InvoiceReadHeader)).scalars().all()
    for header in headers:
        header.invoice_date = None
        header.InvoiceDate = None
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 1
    row = db.get(InvoiceRow, current_row.id)
    assert row.row_status == "blocked_duplicate"
    candidate = db.execute(select(InvoiceDuplicateCandidate)).scalar_one()
    assert candidate.match_status == "blocked_duplicate"
    assert '"invoice_date_match": false' in candidate.evidence_json


def test_strong_cross_batch_duplicate_blocks_when_only_one_side_has_supplier_vat():
    db = _db()
    _tenant, _company, _user, _prior_batch, _prior_run, _prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100", supplier_vat=None)
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 1
    row = db.get(InvoiceRow, current_row.id)
    assert row.row_status == "blocked_duplicate"
    candidate = db.execute(select(InvoiceDuplicateCandidate)).scalar_one()
    assert candidate.match_status == "blocked_duplicate"
    assert '"supplier_name_match": true' in candidate.evidence_json
    assert '"supplier_vat_match": false' in candidate.evidence_json


def test_cross_batch_duplicate_detection_is_tenant_company_isolated():
    db = _db()
    tenant_a, company_a, _user_a = _tenant_company(db, "a")
    tenant_b, company_b, _user_b = _tenant_company(db, "b")
    prior_batch, prior_run = _batch(db, tenant_a, company_a, "Other tenant exported")
    _row(db, prior_batch, prior_run, "INV-200")
    mark_scan_run_completed(db, prior_run, status="processed", page_count=1, row_count=1, notes="prior")
    prior_batch.status = "exported"
    current_batch, current_run = _batch(db, tenant_b, company_b, "Current tenant")
    current_row = _row(db, current_batch, current_run, "INV-200")
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 0
    assert db.get(InvoiceRow, current_row.id).row_status == INVOICE_ROW_STATUS_ACTIVE
    assert db.execute(select(InvoiceDuplicateCandidate)).scalars().all() == []


def test_cross_batch_duplicate_detection_ignores_old_scan_runs():
    db = _db()
    tenant, company, _user = _tenant_company(db)
    prior_batch, run1 = _batch(db, tenant, company, "Prior exported")
    _row(db, prior_batch, run1, "INV-300")
    mark_scan_run_completed(db, run1, status="processed", page_count=1, row_count=1, notes="old")
    db.commit()
    run2 = create_scan_run(db, prior_batch)
    _row(db, prior_batch, run2, "INV-OTHER")
    mark_scan_run_completed(db, run2, status="processed", page_count=1, row_count=1, notes="current")
    prior_batch.status = "exported"
    current_batch, current_run = _batch(db, tenant, company, "Current")
    current_row = _row(db, current_batch, current_run, "INV-300")
    db.commit()

    flagged = detect_cross_batch_duplicates(db, current_batch, current_run.id)

    assert flagged == 0
    assert db.get(InvoiceRow, current_row.id).row_status == INVOICE_ROW_STATUS_ACTIVE
    assert db.execute(select(InvoiceDuplicateCandidate)).scalars().all() == []


def test_cross_batch_duplicate_review_payload_and_override_audit():
    db = _db()
    _tenant, _company, user, _prior_batch, _prior_run, _prior_row, current_batch, current_run = _exported_prior_row(db)
    current_row = _row(db, current_batch, current_run, "INV-100")
    db.commit()
    detect_cross_batch_duplicates(db, current_batch, current_run.id)

    workspace = get_review_workspace(current_batch.id, db=db, user=user)
    row_payload = workspace["rows"][0]
    assert row_payload["blocked_from_export"] is True
    assert "Duplicate of batch 'Prior exported'" in row_payload["row_status_note"]
    assert row_payload["duplicate_candidates"][0]["match_status"] == "blocked_duplicate"
    assert row_payload["explainability"]["duplicates"][0]["candidate_batch_name"] == "Prior exported"

    restored = restore_review_row(current_batch.id, current_row.id, note="Not a duplicate", db=db, user=user)

    assert restored["blocked_from_export"] is False
    assert db.execute(select(InvoiceDuplicateCandidate)).scalar_one().match_status == "overridden"
    actions = [a.action for a in db.execute(select(InvoiceRowFieldAudit)).scalars()]
    assert "row_restore_export" in actions
    assert "cross_batch_duplicate_override" in actions


def test_cross_batch_duplicate_schema_tokens_are_declared():
    model_src = (ROOT / "app/db/review_models.py").read_text()
    service_src = (ROOT / "app/services/cross_batch_duplicates.py").read_text()
    schema_version_src = (ROOT / "app/db/schema_version.py").read_text()
    schema_src = (ROOT / "sql/schema.sql").read_text()
    migration_src = (ROOT / "alembic/versions/2026_07_10_0008_cross_batch_duplicates.py").read_text()
    review_src = (ROOT / "app/routers/review.py").read_text()
    batches_src = (ROOT / "app/routers/batches.py").read_text()
    js_src = (ROOT / "app/static/js/review.js").read_text()

    for token in [
        "invoice_duplicate_candidates",
        "uq_duplicate_candidates_pair_type",
    ]:
        assert token in model_src
        assert token in schema_src
        assert token in migration_src
    for token in ["cross_batch_duplicate", "blocked_duplicate", "review_only"]:
        assert token in service_src
    assert "detect_prior_batch_duplicates" in batches_src
    baseline_gate = batches_src.find("if not provider_baseline_mode:")
    cross_call = batches_src.find("cross_batch_duplicate_count = detect_prior_batch_duplicates", baseline_gate)
    final_status = batches_src.find("Final status", baseline_gate)
    assert baseline_gate != -1 and cross_call != -1 and final_status != -1
    assert cross_call < final_status
    gated_block = batches_src[baseline_gate:cross_call]
    assert "cross_batch_duplicate_count = detect_prior_batch_duplicates" not in gated_block
    assert "cross_batch_duplicate_override" in review_src
    assert "Duplicate check" in js_src
    assert "Duplicate remark" in js_src
    assert "duplicate of" in js_src
    assert 'CURRENT_ALEMBIC_REVISION = "20260710_0010"' in schema_version_src
    assert '"20260710_0009"' in schema_version_src
    assert '"20260710_0008"' in schema_version_src

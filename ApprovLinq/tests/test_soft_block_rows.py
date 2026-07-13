from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Company,
    INVOICE_ROW_STATUS_ACTIVE,
    InvoiceBatch,
    InvoiceFile,
    InvoiceRow,
    ScanRun,
    SupplierPattern,
    Tenant,
    TenantSupplier,
    User,
)
from app.db.review_models import InvoiceFieldCandidate, InvoiceRowCorrection, InvoiceRowFieldAudit
from app.routers.review import delete_review_row, restore_review_row
from app.services.candidate_outcomes import label_batch_candidates
from app.services.corrected_exporter import build_corrected_rows
from app.services.scan_runs import create_scan_run, mark_scan_run_completed
from app.services.supplier_pattern_learning import promote_supplier_patterns_for_batch


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
            InvoiceFile.__table__,
            ScanRun.__table__,
            InvoiceRow.__table__,
            InvoiceFieldCandidate.__table__,
            InvoiceRowCorrection.__table__,
            InvoiceRowFieldAudit.__table__,
            TenantSupplier.__table__,
            SupplierPattern.__table__,
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


def _fixture(db):
    tenant = Tenant(tenant_code="tenant", tenant_name="Tenant")
    db.add(tenant)
    db.flush()
    company = Company(tenant_id=tenant.id, company_code="co", company_name="Company")
    user = User(email="reviewer@example.test", full_name="Reviewer", password_hash="x")
    db.add_all([company, user])
    db.flush()
    supplier = TenantSupplier(
        tenant_id=tenant.id,
        company_id=company.id,
        supplier_name="Supplier Ltd",
        posting_account="SUP001",
        is_active=True,
    )
    db.add(supplier)
    db.flush()
    batch = InvoiceBatch(tenant_id=tenant.id, company_id=company.id, batch_name="Batch", scan_mode="summary")
    db.add(batch)
    db.commit()
    run = create_scan_run(db, batch)
    db.commit()
    return tenant, company, user, supplier, batch, run


def _row(db, batch: InvoiceBatch, run: ScanRun, invoice_number: str, total: float, header: str = "Alpha Beta Gamma Delta") -> InvoiceRow:
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
        header_raw=header,
        review_required=False,
    )
    db.add(row)
    db.flush()
    return row


def test_soft_block_preserves_row_evidence_and_excludes_export_until_restore():
    db = _db()
    _tenant, _company, user, _supplier, batch, run = _fixture(db)
    active = _row(db, batch, run, "ACTIVE", 10.0)
    blocked = _row(db, batch, run, "BLOCKED", 20.0)
    db.add(InvoiceRowCorrection(row_id=blocked.id, batch_id=batch.id, scan_run_id=run.id, row_reviewed=True))
    db.add(InvoiceFieldCandidate(
        id=1,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        batch_id=batch.id,
        scan_run_id=run.id,
        row_id=blocked.id,
        field_name="invoice_number",
        candidate_value="BLOCKED",
        source_type="azure_di",
        selected=True,
        applied=True,
    ))
    mark_scan_run_completed(db, run, status="processed", page_count=1, row_count=2, notes="done")
    db.commit()

    result = delete_review_row(
        batch.id,
        blocked.id,
        row_status="blocked_duplicate",
        reason="duplicate",
        note="review duplicate",
        db=db,
        user=user,
    )

    assert result["deleted"] is False
    assert result["blocked_from_export"] is True
    assert db.get(InvoiceRow, blocked.id).row_status == "blocked_duplicate"
    assert db.get(InvoiceRowCorrection, blocked.id) is not None
    assert db.execute(select(InvoiceFieldCandidate).where(InvoiceFieldCandidate.row_id == blocked.id)).scalar_one()
    assert [r["invoice_number"] for r in build_corrected_rows(db, batch)] == [active.invoice_number]

    restored = restore_review_row(batch.id, blocked.id, note="valid invoice", db=db, user=user)

    assert restored["restored"] is True
    assert db.get(InvoiceRow, blocked.id).row_status == INVOICE_ROW_STATUS_ACTIVE
    assert [r["invoice_number"] for r in build_corrected_rows(db, batch)] == [active.invoice_number, blocked.invoice_number]
    actions = [
        a.action for a in db.execute(
            select(InvoiceRowFieldAudit).where(InvoiceRowFieldAudit.row_id == blocked.id)
        ).scalars()
    ]
    assert "row_soft_block_export" in actions
    assert "row_restore_export" in actions


def test_blocked_rows_are_not_labelled_or_promoted_as_trusted_learning():
    db = _db()
    _tenant, _company, user, _supplier, batch, run = _fixture(db)
    active = _row(db, batch, run, "ACTIVE", 10.0, header="Supplier unique active service words")
    blocked = _row(db, batch, run, "BLOCKED", 20.0, header="Supplier blocked duplicate words")
    db.add_all([
        InvoiceFieldCandidate(
            id=1,
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            batch_id=batch.id,
            scan_run_id=run.id,
            row_id=active.id,
            field_name="invoice_number",
            candidate_value="ACTIVE",
            normalised_value="active",
            source_type="azure_di",
            selected=True,
            applied=True,
        ),
        InvoiceFieldCandidate(
            id=2,
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            batch_id=batch.id,
            scan_run_id=run.id,
            row_id=blocked.id,
            field_name="invoice_number",
            candidate_value="BLOCKED",
            normalised_value="blocked",
            source_type="azure_di",
            selected=True,
            applied=True,
        ),
    ])
    db.commit()
    delete_review_row(batch.id, blocked.id, row_status="blocked_false_positive", db=db, user=user)

    labelled = label_batch_candidates(db, batch=batch, user=user, outcome_source="export")
    promoted = promote_supplier_patterns_for_batch(db, batch=batch, user=user, outcome_source="export")

    candidates = db.execute(select(InvoiceFieldCandidate)).scalars().all()
    assert labelled == 1
    assert [(c.row_id, c.final_value) for c in candidates if c.row_id == blocked.id] == [(blocked.id, None)]
    assert [(c.row_id, c.final_value) for c in candidates if c.row_id == active.id] == [(active.id, "ACTIVE")]
    assert promoted == 1
    pattern = db.execute(select(SupplierPattern)).scalar_one()
    assert pattern.source_row_id == active.id


def test_soft_block_schema_tokens_are_declared():
    model_src = (ROOT / "app/db/models.py").read_text()
    main_src = (ROOT / "app/main.py").read_text()
    migration_src = (ROOT / "alembic/versions/2026_07_10_0007_invoice_row_soft_block.py").read_text()
    schema_src = (ROOT / "sql/schema.sql").read_text()
    for token in [
        "row_status",
        "row_status_reason",
        "row_status_note",
        "row_status_changed_at",
        "row_status_changed_by",
        "ix_invoice_rows_export_status",
    ]:
        assert token in model_src or token == "ix_invoice_rows_export_status"
        assert token in migration_src
        assert token in schema_src
    assert "Base.metadata.create_all" not in main_src

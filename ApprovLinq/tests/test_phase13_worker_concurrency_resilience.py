from __future__ import annotations

from datetime import timedelta
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.db.review_models  # registers review/audit tables
from app.db.models import Base, Company, InvoiceBatch, InvoiceFile, ScanJobPage, Tenant
from app.services.scan_jobs import (
    claim_job_page,
    claim_next_page,
    complete_claimed_page,
    enqueue_scan_job,
    ensure_page_lease_is_current,
    heartbeat_page,
    release_stale_pages,
    request_cancel_job,
    utcnow,
)


ROOT = Path(__file__).resolve().parents[1]


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _batch_with_file(db, *, page_count: int = 2):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="P13", tenant_name="Phase 13 Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="P13C", company_name="Phase 13 Co")
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="Phase 13 Batch",
        status="created",
        scan_mode="summary",
    )
    db.add_all([tenant, company, batch])
    db.flush()
    job = enqueue_scan_job(db, batch)
    invoice_file = InvoiceFile(
        batch_id=batch.id,
        tenant_id=tenant.id,
        company_id=company.id,
        original_filename="invoice.pdf",
        stored_filename="invoice.pdf",
        file_path="invoice.pdf",
        status="uploaded",
        page_count=page_count,
    )
    db.add(invoice_file)
    db.flush()
    from app.services.scan_jobs import initialise_job_pages

    initialise_job_pages(db, job, [invoice_file])
    db.commit()
    return job, invoice_file


def test_atomic_page_claim_allows_only_one_worker_per_page():
    db = _session()
    try:
        job, invoice_file = _batch_with_file(db, page_count=1)

        first = claim_job_page(db, job_id=job.id, source_file_id=invoice_file.id, page_no=1, worker_id="worker-a")
        second = claim_job_page(db, job_id=job.id, source_file_id=invoice_file.id, page_no=1, worker_id="worker-b")

        assert first is not None
        assert first.status == "running"
        assert first.lease_owner == "worker-a"
        assert second is None
    finally:
        db.close()


def test_expired_page_lease_owner_cannot_commit_rows_or_completion():
    db = _session()
    try:
        job, invoice_file = _batch_with_file(db, page_count=1)
        page = claim_job_page(db, job_id=job.id, source_file_id=invoice_file.id, page_no=1, worker_id="slow-worker", lease_seconds=1)
        assert page is not None
        old_token = page.lease_token
        page.lease_until = utcnow() - timedelta(seconds=1)
        db.commit()

        assert complete_claimed_page(db, page_id=page.id, lease_token=old_token, row_count=1) is False
        try:
            ensure_page_lease_is_current(db, page_id=page.id, lease_token=old_token)
        except Exception as exc:
            assert exc.__class__.__name__ == "PageLeaseLost"
        else:
            raise AssertionError("expired page lease should be rejected")

        recovered = release_stale_pages(db)
        reclaimed = claim_job_page(db, job_id=job.id, source_file_id=invoice_file.id, page_no=1, worker_id="new-worker")

        assert recovered == 1
        assert reclaimed is not None
        assert reclaimed.lease_owner == "new-worker"
    finally:
        db.close()


def test_page_completion_persists_provider_telemetry():
    db = _session()
    try:
        job, invoice_file = _batch_with_file(db, page_count=1)
        page = claim_next_page(db, job_id=job.id, worker_id="worker-a")
        assert page is not None
        telemetry = {
            "provider_name": "azure_di",
            "provider_status": "timeout",
            "operation_id": "op-123",
            "request_id": "req-456",
            "latency_ms": 45001,
            "retries": 1,
            "timeout_reason": "Azure DI page timeout after 45s",
        }

        assert complete_claimed_page(db, page_id=page.id, lease_token=page.lease_token, row_count=0, provider_telemetry=telemetry)
        db.commit()
        db.expire_all()

        saved = db.execute(select(ScanJobPage).where(ScanJobPage.id == page.id)).scalar_one()
        assert saved.status == "completed"
        assert saved.provider_name == "azure_di"
        assert saved.provider_status == "timeout"
        assert saved.provider_operation_id == "op-123"
        assert saved.provider_request_id == "req-456"
        assert saved.provider_latency_ms == 45001
        assert saved.provider_retries == 1
        assert saved.provider_timeout_reason == "Azure DI page timeout after 45s"
        assert saved.provider_telemetry["operation_id"] == "op-123"
    finally:
        db.close()


def test_cancellation_marks_queued_pages_and_blocks_running_page_heartbeat():
    db = _session()
    try:
        job, invoice_file = _batch_with_file(db, page_count=2)
        running = claim_job_page(db, job_id=job.id, source_file_id=invoice_file.id, page_no=1, worker_id="worker-a")
        assert running is not None

        assert request_cancel_job(db, job.id) is True
        assert heartbeat_page(db, running.id, lease_token=running.lease_token, lease_seconds=30) is False

        pages = db.execute(select(ScanJobPage).where(ScanJobPage.job_id == job.id)).scalars().all()
        by_page = {page.page_no: page for page in pages}
        assert by_page[1].cancel_requested_at is not None
        assert by_page[2].status == "cancelled"
    finally:
        db.close()


def test_phase13_dockerfile_exposes_separate_worker_target():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM runtime AS worker" in dockerfile
    assert "exec python scripts/scan_worker.py" in dockerfile
    assert "FROM runtime AS web" in dockerfile


def test_phase13_alembic_revision_adds_provider_telemetry_columns():
    migration = (ROOT / "alembic/versions/2026_07_18_0014_worker_concurrency_telemetry.py").read_text(encoding="utf-8")
    for token in (
        "provider_operation_id",
        "provider_request_id",
        "provider_latency_ms",
        "provider_timeout_reason",
        "provider_telemetry",
        "cancel_requested_at",
    ):
        assert token in migration

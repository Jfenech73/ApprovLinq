from __future__ import annotations

from datetime import timedelta
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Company, InvoiceBatch, InvoiceFile, ScanJob, ScanJobPage, Tenant
import app.db.review_models  # registers review/audit tables
from app.services.scan_jobs import (
    claim_next_job,
    enqueue_scan_job,
    get_job_page,
    initialise_job_pages,
    release_stale_jobs,
    request_cancel_job,
    utcnow,
)


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)()


def _batch(db):
    tenant = Tenant(id=uuid.uuid4(), tenant_code="P8", tenant_name="Phase 8 Tenant")
    company = Company(id=uuid.uuid4(), tenant_id=tenant.id, company_code="P8C", company_name="Phase 8 Co")
    batch = InvoiceBatch(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        company_id=company.id,
        batch_name="Phase 8 Batch",
        status="created",
        scan_mode="summary",
    )
    db.add_all([tenant, company, batch])
    db.flush()
    return tenant, company, batch


def test_enqueue_scan_job_creates_durable_job_and_scan_run():
    db = _session()
    try:
        _, _, batch = _batch(db)
        job = enqueue_scan_job(db, batch)
        db.commit()

        assert job.id is not None
        assert job.status == "queued"
        assert job.scan_run_id is not None
        assert batch.current_scan_run_id == job.scan_run_id
        assert batch.status == "processing"
    finally:
        db.close()


def test_atomic_job_claim_allows_only_one_worker():
    db = _session()
    try:
        _, _, batch = _batch(db)
        enqueue_scan_job(db, batch)
        db.commit()

        first = claim_next_job(db, worker_id="worker-a", lease_seconds=60)
        second = claim_next_job(db, worker_id="worker-b", lease_seconds=60)

        assert first is not None
        assert first.status == "claimed"
        assert first.lease_owner == "worker-a"
        assert second is None
    finally:
        db.close()


def test_stale_job_lease_is_released_for_recovery():
    db = _session()
    try:
        _, _, batch = _batch(db)
        job = enqueue_scan_job(db, batch)
        job.status = "running"
        job.lease_owner = "dead-worker"
        job.lease_token = "old-token"
        job.lease_until = utcnow() - timedelta(minutes=5)
        db.commit()

        released = release_stale_jobs(db)
        recovered = db.get(ScanJob, job.id)

        assert released == 1
        assert recovered.status == "queued"
        assert recovered.lease_owner is None
        assert recovered.lease_token is None
    finally:
        db.close()


def test_job_pages_are_idempotent_by_run_file_page():
    db = _session()
    try:
        tenant, company, batch = _batch(db)
        job = enqueue_scan_job(db, batch)
        file_row = InvoiceFile(
            batch_id=batch.id,
            tenant_id=tenant.id,
            company_id=company.id,
            original_filename="invoice.pdf",
            stored_filename="invoice.pdf",
            file_path="invoice.pdf",
            status="uploaded",
            page_count=2,
        )
        db.add(file_row)
        db.flush()

        assert initialise_job_pages(db, job, [file_row]) == 2
        assert initialise_job_pages(db, job, [file_row]) == 0
        db.commit()

        pages = db.execute(select(ScanJobPage).where(ScanJobPage.job_id == job.id)).scalars().all()
        assert sorted(p.page_no for p in pages) == [1, 2]
        assert get_job_page(db, job_id=job.id, source_file_id=file_row.id, page_no=1) is not None
    finally:
        db.close()


def test_cancel_queued_job_is_persisted():
    db = _session()
    try:
        _, _, batch = _batch(db)
        job = enqueue_scan_job(db, batch)
        db.commit()

        assert request_cancel_job(db, job.id) is True
        cancelled = db.get(ScanJob, job.id)
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested_at is not None
    finally:
        db.close()


def test_process_endpoint_enqueues_without_background_task(monkeypatch):
    from app.routers import batches

    batch = type("Batch", (), {"id": uuid.uuid4(), "status": "created", "notes": "", "current_scan_run_id": None})()
    job = type("Job", (), {"id": 42, "scan_run_id": uuid.uuid4()})()

    class FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class FakeDb:
        committed = False

        def query(self, *_args):
            return FakeQuery()

        def commit(self):
            self.committed = True

    monkeypatch.setattr(batches, "_get_batch_for_tenant", lambda db, batch_id, tenant_id: batch)

    def fake_enqueue(db, batch_arg):
        assert batch_arg is batch
        batch.status = "processing"
        return job

    monkeypatch.setattr("app.services.scan_jobs.enqueue_scan_job", fake_enqueue)

    db = FakeDb()
    result = batches.process_batch(batch.id, db=db, tenant_id=uuid.uuid4(), _user=object())

    assert result == {"ok": True, "status": "processing", "job_id": 42, "scan_run_id": str(job.scan_run_id)}
    assert db.committed is True


def test_startup_recovery_does_not_mark_processing_batches_partial():
    src = __import__("pathlib").Path("app/main.py").read_text(encoding="utf-8")
    recover_body = src[src.index("async def recover_stuck_batches"):src.index("@app.on_event(\"startup\")", src.index("async def recover_stuck_batches") + 1)]
    assert "release_stale_jobs" in recover_body
    assert 'batch.status = "partial"' not in recover_body

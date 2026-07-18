"""Durable batch scan orchestration service boundary."""
from __future__ import annotations

import logging
import socket
from uuid import UUID

from app.db.session import SessionLocal
from app.db.models import SCAN_JOB_STATUS_CANCELLED
from app.services.scan_jobs import (
    claim_next_job,
    complete_job,
    fail_job_for_retry,
    mark_job_running,
)

logger = logging.getLogger(__name__)


def process_batch_job(batch_id: UUID, tenant_id) -> None:
    from app.routers.batches import _process_batch_job

    _process_batch_job(batch_id, tenant_id)


def process_next_scan_job(*, worker_id: str | None = None, lease_seconds: int = 300) -> bool:
    """Claim and process one queued scan job.

    Returns True when a job was claimed, False when the queue was empty.
    """
    worker = (worker_id or f"{socket.gethostname()}").strip()[:120]
    db = SessionLocal()
    job = None
    try:
        job = claim_next_job(db, worker_id=worker, lease_seconds=lease_seconds)
        if job is None:
            return False
        mark_job_running(db, job, lease_seconds=lease_seconds)
        db.commit()
        scan_run_id = job.scan_run_id
        job_id = job.id
        batch_id = job.batch_id
        tenant_id = job.tenant_id
    finally:
        db.close()

    try:
        from app.routers.batches import _process_batch_job

        _process_batch_job(batch_id, tenant_id, scan_run_id=scan_run_id, scan_job_id=job_id, worker_id=worker)
    except Exception as exc:
        logger.exception("durable scan job failed job_id=%s batch_id=%s", job_id, batch_id)
        db = SessionLocal()
        try:
            failed_job = db.get(type(job), job_id) if job is not None else None
            if failed_job is not None:
                fail_job_for_retry(db, failed_job, error=str(exc))
                db.commit()
        finally:
            db.close()
        raise

    db = SessionLocal()
    try:
        completed_job = db.get(type(job), job_id) if job is not None else None
        if completed_job is not None:
            if completed_job.status != SCAN_JOB_STATUS_CANCELLED:
                complete_job(db, completed_job)
            db.commit()
    finally:
        db.close()
    return True

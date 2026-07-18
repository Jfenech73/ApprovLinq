from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    InvoiceBatch,
    InvoiceFile,
    ScanJob,
    ScanJobPage,
    SCAN_JOB_ACTIVE_STATUSES,
    SCAN_JOB_PAGE_STATUS_CLAIMED,
    SCAN_JOB_PAGE_STATUS_CANCELLED,
    SCAN_JOB_PAGE_STATUS_COMPLETED,
    SCAN_JOB_PAGE_STATUS_FAILED,
    SCAN_JOB_PAGE_STATUS_QUEUED,
    SCAN_JOB_PAGE_STATUS_RUNNING,
    SCAN_JOB_STATUS_CANCELLED,
    SCAN_JOB_STATUS_CLAIMED,
    SCAN_JOB_STATUS_COMPLETED,
    SCAN_JOB_STATUS_FAILED,
    SCAN_JOB_STATUS_QUEUED,
    SCAN_JOB_STATUS_RUNNING,
)
from app.services.scan_runs import create_scan_run


class PageLeaseLost(RuntimeError):
    """Raised when a worker tries to commit a page it no longer owns."""


class ScanJobCancelled(RuntimeError):
    """Raised at deterministic cancellation checkpoints."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_until(seconds: int) -> datetime:
    return utcnow() + timedelta(seconds=max(1, int(seconds or 1)))


def _is_before(value: datetime | None, boundary: datetime) -> bool:
    if value is None:
        return True
    if value.tzinfo is None and boundary.tzinfo is not None:
        boundary = boundary.replace(tzinfo=None)
    return value < boundary


def _active_job_filter(now: datetime):
    return or_(
        ScanJob.status.in_((SCAN_JOB_STATUS_QUEUED, SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
        and_(ScanJob.status.in_((SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)), ScanJob.lease_until < now),
    )


def enqueue_scan_job(db: Session, batch: InvoiceBatch, *, priority: int = 100, max_attempts: int = 3) -> ScanJob:
    """Create a durable scan job and immutable scan run for a batch.

    If a live job already exists for the batch, return it idempotently.  The
    worker owns execution; the API only persists intent and progress state.
    """
    now = utcnow()
    existing = db.execute(
        select(ScanJob)
        .where(
            ScanJob.batch_id == batch.id,
            ScanJob.status.in_((SCAN_JOB_STATUS_QUEUED, SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
        )
        .order_by(ScanJob.created_at.desc(), ScanJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing:
        return existing

    run = create_scan_run(db, batch)
    job = ScanJob(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        scan_run_id=run.id,
        status=SCAN_JOB_STATUS_QUEUED,
        priority=priority,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    batch.status = "processing"
    batch.page_count = 0
    batch.notes = "Processing queued"
    return job


def claim_next_job(db: Session, *, worker_id: str, lease_seconds: int = 300) -> ScanJob | None:
    now = utcnow()
    candidate = db.execute(
        select(ScanJob.id)
        .where(
            ScanJob.cancel_requested_at.is_(None),
            or_(
                ScanJob.status == SCAN_JOB_STATUS_QUEUED,
                and_(
                    ScanJob.status.in_((SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
                    ScanJob.lease_until.isnot(None),
                    ScanJob.lease_until < now,
                ),
            ),
            ScanJob.attempt_count < ScanJob.max_attempts,
        )
        .order_by(ScanJob.priority.asc(), ScanJob.created_at.asc(), ScanJob.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    lease_token = uuid.uuid4().hex
    result = db.execute(
        update(ScanJob)
        .where(
            ScanJob.id == candidate,
            ScanJob.cancel_requested_at.is_(None),
            or_(
                ScanJob.status == SCAN_JOB_STATUS_QUEUED,
                and_(
                    ScanJob.status.in_((SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
                    ScanJob.lease_until.isnot(None),
                    ScanJob.lease_until < now,
                ),
            ),
            ScanJob.attempt_count < ScanJob.max_attempts,
        )
        .values(
            status=SCAN_JOB_STATUS_CLAIMED,
            attempt_count=ScanJob.attempt_count + 1,
            lease_owner=worker_id[:120],
            lease_token=lease_token,
            lease_until=_lease_until(lease_seconds),
            heartbeat_at=now,
            started_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(ScanJob, candidate)


def mark_job_running(db: Session, job: ScanJob, *, lease_seconds: int = 300) -> None:
    now = utcnow()
    job.status = SCAN_JOB_STATUS_RUNNING
    job.heartbeat_at = now
    job.lease_until = _lease_until(lease_seconds)
    job.updated_at = now
    db.flush()


def heartbeat_job(db: Session, job_id: int, *, lease_token: str | None, lease_seconds: int = 300) -> bool:
    now = utcnow()
    result = db.execute(
        update(ScanJob)
        .where(
            ScanJob.id == job_id,
            ScanJob.status.in_((SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
            ScanJob.lease_token == lease_token,
        )
        .values(heartbeat_at=now, lease_until=_lease_until(lease_seconds), updated_at=now)
    )
    db.commit()
    return result.rowcount == 1


def heartbeat_page(db: Session, page_id: int, *, lease_token: str | None, lease_seconds: int = 300) -> bool:
    now = utcnow()
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.id == page_id,
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
            ScanJobPage.lease_token == lease_token,
            ScanJobPage.cancel_requested_at.is_(None),
        )
        .values(heartbeat_at=now, lease_until=_lease_until(lease_seconds), updated_at=now)
    )
    db.commit()
    return result.rowcount == 1


def complete_job(db: Session, job: ScanJob, *, status: str = SCAN_JOB_STATUS_COMPLETED, error: str | None = None) -> None:
    now = utcnow()
    job.status = status
    job.last_error = (error or "")[:2000] or None
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    job.completed_at = now
    job.updated_at = now
    db.flush()


def fail_job_for_retry(db: Session, job: ScanJob, *, error: str) -> None:
    now = utcnow()
    if job.attempt_count >= job.max_attempts:
        job.status = SCAN_JOB_STATUS_FAILED
        job.completed_at = now
    else:
        job.status = SCAN_JOB_STATUS_QUEUED
    job.last_error = (error or "")[:2000]
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    job.updated_at = now
    db.flush()


def request_cancel_job(db: Session, job_id: int) -> bool:
    now = utcnow()
    job = db.get(ScanJob, job_id)
    if job is None or job.status not in SCAN_JOB_ACTIVE_STATUSES:
        return False
    if job.status == SCAN_JOB_STATUS_QUEUED:
        job.status = SCAN_JOB_STATUS_CANCELLED
    job.cancel_requested_at = now
    job.updated_at = now
    db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.job_id == job_id,
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_QUEUED, SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
        )
        .values(
            cancel_requested_at=now,
            status=case(
                (ScanJobPage.status == SCAN_JOB_PAGE_STATUS_QUEUED, SCAN_JOB_PAGE_STATUS_CANCELLED),
                else_=ScanJobPage.status,
            ),
            updated_at=now,
        )
    )
    db.commit()
    return True


def release_stale_jobs(db: Session, *, stale_before: datetime | None = None) -> int:
    now = stale_before or utcnow()
    result = db.execute(
        update(ScanJob)
        .where(
            ScanJob.status.in_((SCAN_JOB_STATUS_CLAIMED, SCAN_JOB_STATUS_RUNNING)),
            ScanJob.lease_until.isnot(None),
            ScanJob.lease_until < now,
            ScanJob.cancel_requested_at.is_(None),
        )
        .values(
            status=SCAN_JOB_STATUS_QUEUED,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=utcnow(),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def release_stale_pages(db: Session, *, stale_before: datetime | None = None) -> int:
    now = stale_before or utcnow()
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
            ScanJobPage.lease_until.isnot(None),
            ScanJobPage.lease_until < now,
            ScanJobPage.cancel_requested_at.is_(None),
        )
        .values(
            status=SCAN_JOB_PAGE_STATUS_QUEUED,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=utcnow(),
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def initialise_job_pages(db: Session, job: ScanJob, files: Iterable[InvoiceFile]) -> int:
    existing = {
        (row.source_file_id, row.page_no)
        for row in db.execute(select(ScanJobPage).where(ScanJobPage.job_id == job.id)).scalars().all()
    }
    created = 0
    now = utcnow()
    for invoice_file in files:
        for page_no in range(1, int(invoice_file.page_count or 0) + 1):
            key = (invoice_file.id, page_no)
            if key in existing:
                continue
            db.add(ScanJobPage(
                job_id=job.id,
                batch_id=job.batch_id,
                scan_run_id=job.scan_run_id,
                source_file_id=invoice_file.id,
                page_no=page_no,
                status=SCAN_JOB_PAGE_STATUS_QUEUED,
                created_at=now,
                updated_at=now,
            ))
            created += 1
    db.flush()
    return created


def get_job_page(db: Session, *, job_id: int, source_file_id: int, page_no: int) -> ScanJobPage | None:
    return db.execute(
        select(ScanJobPage)
        .where(
            ScanJobPage.job_id == job_id,
            ScanJobPage.source_file_id == source_file_id,
            ScanJobPage.page_no == page_no,
        )
        .limit(1)
    ).scalar_one_or_none()


def claim_next_page(db: Session, *, job_id: int, worker_id: str, lease_seconds: int = 300) -> ScanJobPage | None:
    now = utcnow()
    candidate = db.execute(
        select(ScanJobPage.id)
        .where(
            ScanJobPage.job_id == job_id,
            ScanJobPage.cancel_requested_at.is_(None),
            or_(
                ScanJobPage.status == SCAN_JOB_PAGE_STATUS_QUEUED,
                and_(
                    ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
                    ScanJobPage.lease_until.isnot(None),
                    ScanJobPage.lease_until < now,
                ),
            ),
            ScanJobPage.attempt_count < ScanJobPage.max_attempts,
        )
        .order_by(ScanJobPage.source_file_id.asc(), ScanJobPage.page_no.asc(), ScanJobPage.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    lease_token = uuid.uuid4().hex
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.id == candidate,
            ScanJobPage.cancel_requested_at.is_(None),
            or_(
                ScanJobPage.status == SCAN_JOB_PAGE_STATUS_QUEUED,
                and_(
                    ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
                    ScanJobPage.lease_until.isnot(None),
                    ScanJobPage.lease_until < now,
                ),
            ),
            ScanJobPage.attempt_count < ScanJobPage.max_attempts,
        )
        .values(
            status=SCAN_JOB_PAGE_STATUS_RUNNING,
            attempt_count=ScanJobPage.attempt_count + 1,
            lease_owner=worker_id[:120],
            lease_token=lease_token,
            lease_until=_lease_until(lease_seconds),
            heartbeat_at=now,
            started_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(ScanJobPage, candidate)


def claim_job_page(
    db: Session,
    *,
    job_id: int,
    source_file_id: int,
    page_no: int,
    worker_id: str,
    lease_seconds: int = 300,
) -> ScanJobPage | None:
    now = utcnow()
    candidate = db.execute(
        select(ScanJobPage.id)
        .where(
            ScanJobPage.job_id == job_id,
            ScanJobPage.source_file_id == source_file_id,
            ScanJobPage.page_no == page_no,
            ScanJobPage.cancel_requested_at.is_(None),
            or_(
                ScanJobPage.status == SCAN_JOB_PAGE_STATUS_QUEUED,
                and_(
                    ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
                    ScanJobPage.lease_until.isnot(None),
                    ScanJobPage.lease_until < now,
                ),
            ),
            ScanJobPage.attempt_count < ScanJobPage.max_attempts,
        )
        .limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None
    lease_token = uuid.uuid4().hex
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.id == candidate,
            ScanJobPage.cancel_requested_at.is_(None),
            or_(
                ScanJobPage.status == SCAN_JOB_PAGE_STATUS_QUEUED,
                and_(
                    ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
                    ScanJobPage.lease_until.isnot(None),
                    ScanJobPage.lease_until < now,
                ),
            ),
            ScanJobPage.attempt_count < ScanJobPage.max_attempts,
        )
        .values(
            status=SCAN_JOB_PAGE_STATUS_RUNNING,
            attempt_count=ScanJobPage.attempt_count + 1,
            lease_owner=worker_id[:120],
            lease_token=lease_token,
            lease_until=_lease_until(lease_seconds),
            heartbeat_at=now,
            started_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(ScanJobPage, candidate)


def _clean_provider_telemetry(telemetry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(telemetry, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in telemetry.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[str(key)] = value
        else:
            cleaned[str(key)] = str(value)[:500]
    return cleaned


def _provider_page_values(row_count: int, provider_telemetry: dict[str, Any] | None, *, completed: bool) -> dict[str, Any]:
    now = utcnow()
    telemetry = _clean_provider_telemetry(provider_telemetry)
    values = {
        "status": SCAN_JOB_PAGE_STATUS_COMPLETED if completed else SCAN_JOB_PAGE_STATUS_FAILED,
        "row_count": row_count,
        "lease_owner": None,
        "lease_token": None,
        "lease_until": None,
        "completed_at": now if completed else None,
        "updated_at": now,
        "provider_telemetry": telemetry or None,
        "provider_name": str(telemetry.get("provider_name") or telemetry.get("provider") or "")[:80] or None,
        "provider_status": str(telemetry.get("provider_status") or telemetry.get("status") or "")[:80] or None,
        "provider_operation_id": str(telemetry.get("operation_id") or "")[:160] or None,
        "provider_request_id": str(telemetry.get("request_id") or "")[:160] or None,
        "provider_latency_ms": int(telemetry.get("latency_ms")) if isinstance(telemetry.get("latency_ms"), (int, float)) else None,
        "provider_retries": telemetry.get("retries") if isinstance(telemetry.get("retries"), int) else None,
        "provider_timeout_reason": str(telemetry.get("timeout_reason") or "")[:255] or None,
    }
    return values


def complete_claimed_page(
    db: Session,
    *,
    page_id: int,
    lease_token: str | None,
    row_count: int = 0,
    provider_telemetry: dict[str, Any] | None = None,
) -> bool:
    now = utcnow()
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.id == page_id,
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
            ScanJobPage.lease_token == lease_token,
            ScanJobPage.lease_until.isnot(None),
            ScanJobPage.lease_until >= now,
            ScanJobPage.cancel_requested_at.is_(None),
        )
        .values(**_provider_page_values(row_count, provider_telemetry, completed=True))
        .execution_options(synchronize_session=False)
    )
    db.flush()
    return result.rowcount == 1


def fail_claimed_page_for_retry(
    db: Session,
    *,
    page_id: int,
    lease_token: str | None,
    error: str,
    provider_telemetry: dict[str, Any] | None = None,
) -> bool:
    page = db.get(ScanJobPage, page_id)
    if page is None:
        return False
    now = utcnow()
    exhausted = int(page.attempt_count or 0) >= int(page.max_attempts or 3)
    values = _provider_page_values(0, provider_telemetry, completed=False)
    values.update(
        status=SCAN_JOB_PAGE_STATUS_FAILED if exhausted else SCAN_JOB_PAGE_STATUS_QUEUED,
        last_error=(error or "")[:2000],
        completed_at=now if exhausted else None,
    )
    result = db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.id == page_id,
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
            ScanJobPage.lease_token == lease_token,
            ScanJobPage.lease_until.isnot(None),
            ScanJobPage.lease_until >= now,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    return result.rowcount == 1


def ensure_page_lease_is_current(db: Session, *, page_id: int, lease_token: str | None) -> None:
    now = utcnow()
    current = db.get(ScanJobPage, page_id)
    if (
        current is None
        or current.lease_token != lease_token
        or current.lease_until is None
        or _is_before(current.lease_until, now)
        or current.cancel_requested_at is not None
    ):
        raise PageLeaseLost("Current worker no longer owns this scan page lease")


def job_cancel_requested(db: Session, job_id: int | None) -> bool:
    if job_id is None:
        return False
    job = db.get(ScanJob, job_id)
    return bool(job and job.cancel_requested_at is not None)


def raise_if_job_cancelled(db: Session, job_id: int | None) -> None:
    if job_cancel_requested(db, job_id):
        raise ScanJobCancelled("Scan job cancellation requested")


def mark_job_cancelled(db: Session, job: ScanJob | None) -> None:
    if job is None:
        return
    now = utcnow()
    job.status = SCAN_JOB_STATUS_CANCELLED
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    job.completed_at = now
    job.updated_at = now
    db.execute(
        update(ScanJobPage)
        .where(
            ScanJobPage.job_id == job.id,
            ScanJobPage.status.in_((SCAN_JOB_PAGE_STATUS_QUEUED, SCAN_JOB_PAGE_STATUS_CLAIMED, SCAN_JOB_PAGE_STATUS_RUNNING)),
        )
        .values(
            status=SCAN_JOB_PAGE_STATUS_CANCELLED,
            cancel_requested_at=now,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            updated_at=now,
        )
    )
    db.flush()


def mark_page_running(db: Session, page: ScanJobPage, *, worker_id: str | None = None, lease_seconds: int = 300) -> None:
    now = utcnow()
    page.status = SCAN_JOB_PAGE_STATUS_RUNNING
    page.attempt_count = int(page.attempt_count or 0) + 1
    page.lease_owner = (worker_id or "")[:120] or None
    page.lease_token = uuid.uuid4().hex
    page.lease_until = _lease_until(lease_seconds)
    page.heartbeat_at = now
    page.started_at = page.started_at or now
    page.updated_at = now
    db.flush()


def mark_page_completed(db: Session, page: ScanJobPage, *, row_count: int = 0) -> None:
    now = utcnow()
    page.status = SCAN_JOB_PAGE_STATUS_COMPLETED
    page.row_count = row_count
    page.lease_owner = None
    page.lease_token = None
    page.lease_until = None
    page.completed_at = now
    page.updated_at = now
    db.flush()


def mark_page_failed_for_retry(db: Session, page: ScanJobPage, *, error: str) -> None:
    now = utcnow()
    if int(page.attempt_count or 0) >= int(page.max_attempts or 3):
        page.status = SCAN_JOB_PAGE_STATUS_FAILED
        page.completed_at = now
    else:
        page.status = SCAN_JOB_PAGE_STATUS_QUEUED
    page.last_error = (error or "")[:2000]
    page.lease_owner = None
    page.lease_token = None
    page.lease_until = None
    page.updated_at = now
    db.flush()

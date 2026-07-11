"""Batch scan orchestration service boundary for Phase 7.

The durable job queue belongs to Phase 8.  This service simply owns the
background-task entry point while delegating to the current in-process job.
"""
from __future__ import annotations

from uuid import UUID


def process_batch_job(batch_id: UUID, tenant_id) -> None:
    from app.routers.batches import _process_batch_job

    _process_batch_job(batch_id, tenant_id)

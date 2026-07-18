from __future__ import annotations

import argparse
import logging
import time

from app.db.session import SessionLocal
from app.config import settings
from app.services.scan_jobs import release_stale_jobs, release_stale_pages
from app.services.scan_orchestrator import process_next_scan_job


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the durable ApprovLinq scan worker.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    parser.add_argument("--sleep-seconds", type=float, default=float(getattr(settings, "scan_worker_sleep_seconds", 5.0)), help="Idle sleep between queue polls.")
    parser.add_argument("--lease-seconds", type=int, default=int(getattr(settings, "scan_worker_lease_seconds", 300)), help="Job/page lease duration.")
    parser.add_argument("--worker-id", default=None, help="Stable worker identifier.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    while True:
        db = SessionLocal()
        try:
            release_stale_jobs(db)
            release_stale_pages(db)
        finally:
            db.close()
        claimed = process_next_scan_job(worker_id=args.worker_id, lease_seconds=args.lease_seconds)
        if args.once:
            return
        if not claimed:
            time.sleep(max(0.5, args.sleep_seconds))


if __name__ == "__main__":
    main()

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager

from app.config import settings


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context_bits: list[str] = []
        for key in (
            "batch_id",
            "tenant_id",
            "file_name",
            "page_no",
            "stage",
            "status",
            "duration_ms",
            "supplier_match_score",
            "supplier_match_type",
        ):
            value = getattr(record, key, None)
            if value not in (None, ""):
                context_bits.append(f"{key}={value}")
        if context_bits:
            return f"{base} | " + " ".join(context_bits)
        return base


def configure_logging() -> None:
    root_logger = logging.getLogger()
    if getattr(configure_logging, "_configured", False):
        root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        ContextFormatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    )
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    configure_logging._configured = True


@contextmanager
def log_timed_stage(logger: logging.Logger, message: str, *, level: int = logging.INFO, **extra):
    started = time.perf_counter()
    logger.log(level, f"{message} started", extra=extra)
    try:
        yield
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        end_extra = dict(extra)
        end_extra["duration_ms"] = duration_ms
        logger.log(level, f"{message} completed", extra=end_extra)

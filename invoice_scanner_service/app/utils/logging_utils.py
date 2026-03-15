from __future__ import annotations

import logging
import sys
import time
from typing import Any

_CONFIGURED = False


def configure_logging(level_name: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def now_ms() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    if fields:
        field_text = " | ".join(f"{key}={value}" for key, value in fields.items())
        logger.log(level, "%s | %s", message, field_text)
    else:
        logger.log(level, message)

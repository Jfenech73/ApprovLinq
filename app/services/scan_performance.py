"""Lightweight scan performance helpers.

All logging from this module is intentionally safe: it records identifiers,
timings, counts, and method names only. It never records invoice contents,
full OCR text, secrets, or extracted field values.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Iterator


PERFORMANCE_LOG_SAFE_FIELDS = {
    "batch_id", "file_id", "page_no", "timing", "counts", "method_names"
}


@dataclass
class ScanPerformanceContext:
    batch_id: Any
    timings: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    method_counts: dict[str, int] = field(default_factory=dict)
    saved_region_cache: dict[tuple[Any, Any], list[Any]] = field(default_factory=dict)
    saved_region_read_cache: dict[tuple[Any, ...], tuple[str, int | None, list[int]]] = field(default_factory=dict)
    supplier_history_cache: dict[tuple[Any, Any, str], Any] = field(default_factory=dict)
    audit_seen: set[tuple[Any, ...]] = field(default_factory=set)

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + (perf_counter() - start)

    def inc(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + int(amount)

    def inc_method(self, method_name: str, amount: int = 1) -> None:
        key = str(method_name or "unknown").split("+")[0][:80]
        self.method_counts[key] = self.method_counts.get(key, 0) + int(amount)

    def timing_ms(self, name: str) -> int:
        return int(round(self.timings.get(name, 0.0) * 1000))

    def seconds(self, name: str) -> float:
        return round(self.timings.get(name, 0.0), 4)

    def summary(self, pages_processed: int, rows_created: int, review_required_count: int) -> dict[str, Any]:
        elapsed = self.timings.get("total_batch_processing", 0.0)
        return {
            "pages_processed": int(pages_processed or 0),
            "rows_created": int(rows_created or 0),
            "method_counts": dict(sorted(self.method_counts.items())),
            "saved_regions_tested": int(self.counts.get("saved_regions_tested", 0)),
            "saved_regions_applied": int(self.counts.get("saved_regions_applied", 0)),
            "saved_regions_conflicted": int(self.counts.get("saved_regions_conflicted", 0)),
            "candidates_persisted": int(self.counts.get("candidates_persisted", 0)),
            "review_required_count": int(review_required_count or 0),
            "total_elapsed_seconds": round(elapsed, 3),
            "average_seconds_per_page": round(elapsed / pages_processed, 3) if pages_processed else 0.0,
            "timings_seconds": {k: round(v, 4) for k, v in sorted(self.timings.items())},
        }

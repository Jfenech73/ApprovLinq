from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from app.services.extractor import simple_extract


ROOT = Path(__file__).resolve().parents[1]


def _normalise(value):
    if isinstance(value, date):
        return value.isoformat()
    return value


def test_redacted_golden_invoice_baseline_is_repeatable():
    pack = json.loads((ROOT / "tests/golden/phase5_redacted_baseline.json").read_text(encoding="utf-8"))
    assert pack["pack"] == "phase5-redacted-golden-invoice-regression"
    assert pack["records"]

    for record in pack["records"]:
        started = time.perf_counter()
        result = simple_extract(record["text"])
        elapsed_ms = (time.perf_counter() - started) * 1000

        expected = record["expected"]
        for field, expected_value in expected.items():
            result_key = "_deposit_candidate" if field == "deposit_candidate" else field
            assert _normalise(result.get(result_key)) == expected_value, record["case_id"]

        assert elapsed_ms <= record["baseline_metrics"]["max_runtime_ms"], record["case_id"]

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_performance_logging_helper_exists_and_uses_safe_fields():
    src = read("app/services/scan_performance.py")
    assert "class ScanPerformanceContext" in src
    assert "def timed" in src
    assert "def summary" in src
    for token in [
        "extraction_provider",
        "rule_application",
        "saved_region_replay",
        "supplier_history_lookup",
        "totals_reconciliation",
        "arbitration",
        "candidate",
        "db_commit",
        "total_page_processing",
        "total_batch_processing",
    ]:
        assert token in src or token in read("app/routers/batches.py") or token in read("app/services/invoice_arbitration.py")
    assert "PERFORMANCE_LOG_SAFE_FIELDS" in src
    assert "full OCR text" in src


def test_saved_region_cache_excludes_archived_deleted_and_failed_fallbacks():
    src = read("app/routers/batches.py")
    assert "def _get_active_saved_regions_for_batch" in src
    assert "saved_region_cache" in src
    assert "RemapHint.archived.is_(False)" in src
    assert "RemapHint.deleted_at.is_(None)" in src
    assert "def _saved_region_should_skip_for_performance" in src
    assert "success_count == 0 and failure_count >= 5" in src
    assert "getattr(hint, \"is_primary\", False)" in src


def test_primary_region_is_tried_before_fallback_and_page_search_is_capped():
    src = read("app/routers/batches.py")
    assert "matched = sorted(matched, key=_hint_priority)" in src
    assert "0 if getattr(h, \"is_primary\", False) else 1" in src
    assert "[:5]  # cap page search safely" in src
    assert "page_no is reference only" in src or "Page number is a reference" in src


def test_candidate_persistence_dedupes_ignores_blanks_truncates_and_bulk_adds():
    src = read("app/services/invoice_arbitration.py")
    assert "seen: set[tuple[Any, ...]]" in src
    assert "not value_text or not str(value_text).strip()" in src
    assert "bulk_candidates" in src
    assert "db.add_all(bulk_candidates)" in src
    assert "[:1000]" in src
    assert "candidates_persisted" in src


def test_noop_audit_suppression_preserves_conflict_and_suggest_audits():
    arb = read("app/services/invoice_arbitration.py")
    batches = read("app/routers/batches.py")
    assert "old_text == new_text" in arb
    assert "arbitration_conflict" in arb
    assert "arbitration_suggest" in arb
    assert "audit_seen" in arb
    assert "saved_region_conflict" in batches
    assert "saved_region_apply" in batches


def test_analytics_endpoint_remains_dashboard_only_not_called_during_scan():
    batches = read("app/routers/batches.py")
    analytics = read("app/routers/analytics.py")
    assert "@router.get" in analytics
    scan_body = batches[batches.index("def _process_batch_job"):]
    assert "/analytics" not in scan_body
    assert "analytics" not in scan_body.lower()

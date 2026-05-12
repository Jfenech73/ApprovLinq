from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding='utf-8')


def test_remap_hint_governance_columns_exist():
    src = read('app/db/review_models.py')
    for token in [
        'is_primary', 'archived', 'archived_at', 'deleted_at', 'superseded_by_hint_id',
        'last_used_at', 'last_used_batch_id', 'last_used_row_id', 'last_used_page_no',
        'last_read_text', 'last_result', 'success_count', 'failure_count',
        'conflict_count', 'apply_count',
    ]:
        assert token in src


def test_startup_schema_has_governance_alters():
    src = read('app/main.py')
    assert 'ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS is_primary' in src
    assert 'ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS archived' in src
    assert 'ALTER TABLE remap_hints ADD COLUMN IF NOT EXISTS last_result' in src
    assert 'ix_remap_governance' in src


def test_review_endpoints_for_region_governance_exist():
    src = read('app/routers/review.py')
    for route in [
        '@router.post("/remap-hints/{hint_id}/primary")',
        '@router.post("/remap-hints/{hint_id}/archive")',
        '@router.post("/remap-hints/{hint_id}/restore")',
        '@router.post("/remap-hints/deduplicate")',
        'hard_delete: bool = Query(default=False)',
        'Archive this saved region before permanent deletion',
    ]:
        assert route in src


def test_saved_region_application_filters_and_prioritises_governed_regions():
    src = read('app/routers/batches.py')
    assert 'RemapHint.archived.is_(False)' in src
    assert 'RemapHint.deleted_at.is_(None)' in src
    assert 'getattr(h, "is_primary", False)' in src
    assert 'success_count' in src
    assert 'last_used_at' in src
    assert 'saved_region_apply' in src
    assert 'saved_region_conflict' in src


def test_ui_surfaces_primary_fallback_archive_actions():
    review_js = read('app/static/js/review.js')
    rules_js = read('app/static/js/rules.js')
    for token in ['data-primary-region', 'data-archive-region', 'data-restore-region', 'data-hard-delete-region']:
        assert token in review_js
    assert 'primary saved region' in rules_js
    assert 'fallback saved region' in rules_js
    assert 'archived' in rules_js

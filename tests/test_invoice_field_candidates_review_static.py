from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_review_candidates_endpoint_exists_and_uses_tenant_access():
    src = (ROOT / 'app' / 'routers' / 'review.py').read_text()
    assert '@router.get("/batches/{batch_id}/rows/{row_id}/candidates")' in src
    assert 'InvoiceFieldCandidate' in src
    assert '_require_batch_access(db, user, batch, x_tenant_id)' in src
    assert 'Batch does not belong to selected tenant' in src
    assert 'Not authorised for this batch' in src


def test_review_candidates_endpoint_groups_by_field_and_returns_required_shape():
    src = (ROOT / 'app' / 'routers' / 'review.py').read_text()
    for key in [
        'candidate_value', 'source_type', 'confidence', 'evidence', 'reason',
        'selected', 'applied', 'conflict', 'rejected_reason', 'created_at'
    ]:
        assert f'"{key}"' in src
    assert 'grouped.setdefault(c.field_name' in src
    assert 'return {"batch_id": str(batch.id), "row_id": row.id, "fields": grouped}' in src


def test_review_js_fetches_and_renders_persisted_candidates():
    src = (ROOT / 'app' / 'static' / 'js' / 'review.js').read_text()
    assert 'loadCandidateEvidence' in src
    assert '/candidates' in src
    assert 'persisted_candidates' in src
    assert 'Persisted arbitration candidates' in src
    for badge in ['Selected', 'Applied', 'Rejected', 'Conflict', 'Suggested']:
        assert badge in src

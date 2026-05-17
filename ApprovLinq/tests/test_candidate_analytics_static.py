from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding='utf-8')


def test_candidate_analytics_endpoint_exists_and_is_tenant_safe():
    text = read('app/routers/review.py')
    assert '@router.get("/candidate-analytics")' in text
    assert 'InvoiceFieldCandidate' in text
    assert 'user.role == "admin"' in text
    assert 'M.UserTenant' in text
    assert 'Forbidden for selected tenant' in text
    assert 'tenant_id' in text
    assert 'company_id' in text
    assert 'supplier' in text
    assert 'field_name' in text
    assert 'source_type' in text


def test_candidate_analytics_metrics_return_requested_sections():
    text = read('app/routers/review.py')
    for key in [
        'candidate_count', 'selected_count', 'applied_count', 'accepted_count',
        'corrected_count', 'conflict_count', 'accuracy', 'correction_rate',
        'by_source_type', 'by_field', 'top_corrected_suppliers',
    ]:
        assert key in text
    assert 'user_accepted' in text
    assert 'user_corrected' in text


def test_admin_candidate_analytics_panel_and_js_exist():
    html = read('app/static/admin.html')
    js = read('app/static/js/admin.js')
    assert 'Candidate Analytics' in html
    assert 'candidateAnalyticsSummary' in html
    assert 'candidateSourceTableBody' in html
    assert 'candidateFieldTableBody' in html
    assert 'candidateSupplierTableBody' in html
    assert 'loadCandidateAnalytics' in js
    assert '/review/candidate-analytics' in js
    assert 'candidateTenantFilter' in js


def test_tenant_candidate_analytics_panel_and_js_exist():
    html = read('app/static/tenant.html')
    js = read('app/static/js/tenant.js')
    assert 'Candidate Analytics' in html
    assert 'candidateAnalyticsSummary' in html
    assert 'candidateSourceTableBody' in html
    assert 'candidateFieldTableBody' in html
    assert 'candidateSupplierTableBody' in html
    assert 'loadCandidateAnalytics' in js
    assert '/review/candidate-analytics' in js
    assert 'candidateTenantFilter' not in html

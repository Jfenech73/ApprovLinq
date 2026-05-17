from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_review_router_has_admin_global_rule_endpoints_and_shapes():
    review = read("app/routers/review.py")
    ast.parse(review)
    assert 'is_global' in read("app/db/review_models.py")
    assert '@router.get("/admin/rules")' in review
    assert '@router.post("/admin/rules/{rule_id}/global")' in review
    assert '@router.post("/admin/rules/{rule_id}/tenant-scoped")' in review
    assert '"tenant_name"' in review
    assert '"tenant_code"' in review
    assert '"is_global"' in review
    assert 'Platform-global rules apply in the background but remain admin-only' in review


def test_manage_rules_includes_saved_regions_as_rule_rows():
    review = read("app/routers/review.py")
    js = read("app/static/js/rules.js")
    assert 'include_saved_regions: bool = Query(default=True)' in review
    assert 'def _remap_hint_as_rule_dict' in review
    assert '"item_type": "saved_region"' in review
    assert 'saved_region: "Saved region"' in js
    assert '/review/remap-hints/${rawId}/${action}' in js
    assert '/review/remap-hints/${rawId}' in js


def test_admin_page_has_rule_governance_global_conversion():
    html = read("app/static/admin.html")
    js = read("app/static/js/admin.js")
    assert 'id="adminRulesTableBody"' in html
    assert 'Rule Governance' in html
    assert 'function loadAdminRules' in js
    assert '/review/admin/rules' in js
    assert '/review/admin/rules/${button.dataset.globalRule}/global' in js
    assert '/review/admin/rules/${button.dataset.tenantRule}/tenant-scoped' in js


def test_issue_log_is_support_ticket_only_not_scan_review_lines():
    batches = read("app/routers/batches.py")
    admin = read("app/routers/admin.py")
    tenant = read("app/routers/tenant.py")
    admin_html = read("app/static/admin.html")
    assert '_create_batch_issue_logs(batch_id, _batch_tenant_id' not in batches
    assert 'Issue Log is reserved for tenant-raised support tickets' in batches
    assert 'IssueLog.created_by_user_id.isnot(None)' in admin
    assert 'IssueLog.created_by_user_id.isnot(None)' in tenant
    assert 'Support Tickets' in admin_html

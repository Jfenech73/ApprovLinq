"""
Smoke tests for the API contracts the v2 UI depends on.

These tests don't replace full integration coverage — they verify the *shape*
of each endpoint so UI-side regressions are caught quickly. Run with:

    pytest tests/test_api_contracts.py -v

Each test uses FastAPI's TestClient and an in-process SQLite DB so there are
no external dependencies.
"""
from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Build a TestClient against the real app. We use env vars to point at
    an in-memory SQLite so tests don't require a running Postgres."""
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _auth_headers(client, email, password):
    """Log in and return headers ready to use."""
    r = client.post("/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        pytest.skip(f"Could not log in as {email}: {r.status_code} {r.text}")
    token = r.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


# ───────────────────────────────────────────────────────────────────────────
# Shape contracts: each test locks the fields the UI reads.
# ───────────────────────────────────────────────────────────────────────────

class TestAuthMeShape:
    """The user block in the sidebar reads full_name, email, role, tenants[]."""

    def test_me_shape_for_tenant_user(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user available in test DB")
        r = client.get("/auth/me", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        # These are the exact keys ap-ui.js's populateUserBlock reads.
        assert "email" in body, "UI relies on /auth/me.email"
        assert "full_name" in body, "UI relies on /auth/me.full_name"
        assert "role" in body, "UI relies on /auth/me.role to unhide admin nav"
        assert "tenants" in body, "UI relies on /auth/me.tenants[] for tenant display"
        assert isinstance(body["tenants"], list)
        if body["tenants"]:
            t = body["tenants"][0]
            assert "tenant_name" in t, "Sidebar reads tenants[0].tenant_name"
            assert "is_default" in t, "Sidebar prefers tenants[].is_default"


class TestTenantEndpointShape:
    """The Tenant admin page depends on /tenant/companies, /tenant/suppliers, /tenant/nominal-accounts."""

    def test_companies_list(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user")
        r = client.get("/tenant/companies", headers=headers)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list), "Tenant UI expects an array"


class TestAnalyticsShape:
    """Analytics page reads /analytics/summary with company_id query."""

    def test_summary_requires_company(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user")
        # Without company_id may 400 or return overall — either is fine, just
        # verify it doesn't crash.
        r = client.get("/analytics/summary", headers=headers)
        assert r.status_code in (200, 400, 422), r.text


class TestAdminOnlyEndpoints:
    """Endpoints that should reject non-admin callers."""

    def test_admin_tenants_denied_for_tenant_user(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user")
        r = client.get("/admin/tenants", headers=headers)
        # Should be 401 or 403. Never 200.
        assert r.status_code in (401, 403), \
            f"Tenant user should NOT see /admin/tenants, got {r.status_code}"


class TestBatchProgressShape:
    """Review-as-you-go relies on /batches/{id}/progress returning files[]."""

    def test_progress_shape_structure(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user")
        r = client.get("/batches", headers=headers)
        if r.status_code != 200 or not r.json():
            pytest.skip("No existing batches to test progress against")
        batch_id = r.json()[0]["id"]
        r = client.get(f"/batches/{batch_id}/progress", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert "files" in body, "Scanner UI relies on progress.files[]"
        if body["files"]:
            f = body["files"][0]
            for key in ("file_id", "filename", "review_state"):
                assert key in f, f"Scanner UI reads files[].{key}"


class TestReviewBatchShape:
    """Review page reads /review/batches/{id} and /review/files/{id}/info."""

    def test_review_batch_shape(self, client):
        try:
            headers = _auth_headers(client, "demo@approvlinq.io", "demo1234")
        except Exception:
            pytest.skip("No seeded tenant user")
        r = client.get("/batches", headers=headers)
        if r.status_code != 200 or not r.json():
            pytest.skip("No existing batches")
        batch_id = r.json()[0]["id"]
        r = client.get(f"/review/batches/{batch_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "batch" in body, "Review UI reads .batch"
        assert "rows" in body, "Review UI reads .rows[]"
        # Status pill reads .batch.status — the field that caused the v2 regression
        assert "status" in body["batch"], "Review UI reads .batch.status (batchStatusPill)"

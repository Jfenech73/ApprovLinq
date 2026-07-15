from __future__ import annotations

import os


def pytest_configure(config):
    """Keep unit tests isolated from developer/deployment environment variables."""
    if os.getenv("APPROVLINQ_USE_EXTERNAL_TEST_DATABASE") != "1":
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only")

# ApprovLinq 3.69.14 Release Manifest

Release date: 2026-07-18

Package: `ApprovLinq_3_69_phase14_governance_analytics_hardening.zip`

Alembic head: `20260718_0015`

## Scope

- Phase 14 only: governance and analytics hardening.
- Learning recommendation RBAC and separation of duties.
- Mandatory canary pass before promotion.
- Rollback metadata retained on promotions.
- FX-rate provenance model.
- Explicit BCRS/deposit fact component provenance.
- Exact calendar-month analytics windows.
- SQL-backed VAT exception and credit-note analytics.
- Facts-versus-hypotheses narrative contract for insight responses.

## Behaviour Preserved

- No extraction, invoice-processing, arbitration, saved-region matching, totals reconciliation, BCRS split, review, or corrected export row-building behaviour was intentionally changed.
- Existing export/fact reconciliation remains source-total based for the approved corrected export snapshot.

## Required Gate Results

- Compilation: passed locally with `python -m compileall app tests`.
- Test collection/regression: passed locally with `pytest -q`.
- PostgreSQL clean/stable migration tests: present and mandatory when `APPROVLINQ_POSTGRES_TEST_URL`, `APPROVLINQ_POSTGRES_STABLE_TEST_URL`, and `APPROVLINQ_ALLOW_DESTRUCTIVE_TEST_DB=1` are configured.
- Docker/container scan: not run in this local environment unless Docker is installed.

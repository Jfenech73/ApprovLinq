# ApprovLinq 3.69.11 Release Manifest

Date: 2026-07-15
Phase: 11 - Release stabilisation and regression closure

## Scope

- Feature work frozen.
- Regression suite collection error closed.
- Brittle source-slicing/string tests replaced with functional or architecture tests.
- PostgreSQL clean-migration and stable-upgrade integration gates present.
- Deterministic ZIP packaging added with an `ApprovLinq/` top-level folder for Docker build context compatibility.
- Release hygiene, secret scanning, golden regression, compile, and startup/configuration tests run locally.

## Behaviour Preserved

- No intentional changes to invoice extraction behavior.
- No intentional changes to arbitration, saved regions, totals, BCRS, review, or export workflows.
- Supplier saved-region confirmation remains guarded to prevent unrelated supplier overwrites.
- Deposit/BCRS mismatch reasons remain review-visible and are not auto-approved.

## Local Gate Results

- `pytest -q --tb=short`: 1049 passed, 11 skipped, 242 warnings.
- `python -m compileall app scripts tests`: passed.
- `pytest -q tests/test_release_hygiene.py tests/test_schema_foundation.py tests/test_phase5_golden_regression.py --tb=short`: 16 passed, 2 skipped after the stable-upgrade gate was added.
- `alembic heads`: `20260713_0013 (head)`.
- Startup smoke with `DATABASE_URL=sqlite:///:memory:`: imported `app.main`, title `Invoice Scanner Service`, 119 routes.
- Startup smoke against the shell's existing database URL: failed as expected because the database was not Alembic-stamped.
- `python -m pip check`: failed in the local desktop environment because installed global packages conflict:
  - `jupyterlab 4.3.2` requires `httpx~=0.28.0`, but `httpx 0.27.2` is installed.
  - `pandasai 2.0.43` requires `openai<2`, but `openai 2.21.0` is installed.

## Environment-Blocked Gates

- Docker build and container scan were not run locally because Docker is not installed in this shell.
- PostgreSQL clean and stable-upgrade integration tests were skipped locally because no destructive PostgreSQL test URL was configured.

Required PostgreSQL gate environment:

- `APPROVLINQ_POSTGRES_TEST_URL`
- Optional separate `APPROVLINQ_POSTGRES_STABLE_TEST_URL`
- `APPROVLINQ_ALLOW_DESTRUCTIVE_TEST_DB=1`

## Packaging

- Build script: `scripts/build_release_zip.py`
- Default artifact: `dist/ApprovLinq_3_69_phase11_release_stabilisation_regression_closure.zip`
- ZIP entries are sorted, timestamp-normalised, and rooted under `ApprovLinq/`.
- Excludes local env files, runtime data, caches, databases, private keys, and existing ZIPs.

## Rollback

1. Restore the previous Phase 10 artifact: `ApprovLinq_3_69_phase10_approved_invoice_fact_expense_insights.zip`.
2. Redeploy the previous artifact with the same environment variables.
3. If database migrations were run from this release, verify Alembic head compatibility before downgrading; this phase adds no new database migration.
4. Re-run the Phase 10 smoke tests and golden extraction comparison after rollback.

# ApprovLinq 3.69.12 Release Manifest

Date: 2026-07-17
Phase: 12 - Approval, evidence and fact integrity

## Scope

- Central `ExportEligibilityPolicy`.
- Mandatory unresolved-row, page-error, file-error, scan-page-error and approval checks.
- Immutable/export-event-backed approved invoice fact creation.
- Batch archive instead of physical delete.
- Durable evidence retention and fact drill-down.
- Reconciliation from corrected export rows to approved fact snapshots and insight totals.
- Prevention of unresolved rows becoming trusted learning evidence.

## Behaviour Preserved

- No intentional change to extraction, OCR, DI, arbitration, saved-region resolution, BCRS split logic, or row correction semantics.
- Soft-blocked rows remain excluded from export.
- Approved facts are still computed from corrected export overlays, not mutable raw rows.

## Integrity Gates

- Export requires batch status `approved` or `exported`, active exportable rows, no unresolved active rows, no page-error rows, no failed/partial files, and no failed scan-job pages.
- Approved fact materialisation requires a persisted matching `BatchExportEvent` with durable file evidence.
- Fact totals are reconciled against the corrected export snapshot before materialisation completes.
- Batch delete endpoint now writes an archive audit event and leaves rows, files, export events and facts in place.
- Insight aggregate rows include immutable `fact_ids`, and `/analytics/facts/{fact_id}` returns fact and evidence drill-down.
- Candidate labels, supplier-pattern promotion and learning recommendations reject unresolved/non-exportable rows as trusted evidence.

## Local Gate Results

- `pytest -q --tb=short`: 1054 passed, 11 skipped, 249 warnings.
- Focused Phase 12/approval/fact/learning/archive suite: 30 passed, 2 skipped.
- Compile checks: `python -m py_compile` for changed modules passed.

## Database Migrations

- No new Alembic migration is required for this phase.
- Batch archival uses existing batch status plus audit rows.
- Fact/evidence drill-down uses existing approved fact, export event, row and read-header tables.

## Packaging

- Build script: `scripts/build_release_zip.py`
- Default artifact: `dist/ApprovLinq_3_69_phase12_approval_evidence_fact_integrity.zip`
- ZIP entries are sorted, timestamp-normalised, rooted under `ApprovLinq/`, and exclude secrets/runtime artifacts.

## Rollback

1. Redeploy `ApprovLinq_3_69_phase11_release_stabilisation_regression_closure.zip`.
2. No schema downgrade is required because Phase 12 adds no migration.
3. Re-export only approved batches after rollback if any archive/export actions were performed under Phase 12.
4. Previously materialised approved facts remain in the database; verify reporting expectations before purging any retained evidence manually.

# ApprovLinq Changelog

## v3.69.14 - 2026-07-18

### Governance and analytics hardening
- Added learning recommendation RBAC checks and separation-of-duties controls for run, approve, canary, promote, and rollback actions.
- Enforced canary evidence before learning proposals can be promoted, with promotion snapshots retaining rollback and canary metadata.
- Added tenant-scoped FX-rate provenance and explicit BCRS/deposit fact component provenance to approved invoice facts.
- Reworked exception analytics to use SQL filtering/aggregation and exact calendar-month windows.
- Added a facts-versus-hypotheses narrative contract to insight summary, exception, duplicate exposure, variance, and fact drill-down responses.

## v3.69.13 - 2026-07-18

### Worker concurrency and operational resilience
- Added worker page lease fencing, heartbeat checks, cancellation checkpoints, and idempotent provider attempt telemetry.
- Added persisted Azure/provider operation telemetry fields for request IDs, operation IDs, latency, retry count, timeout reason, and raw telemetry payloads.
- Added a separate scan worker entry point and Docker worker target for operational separation.

## v3.69.12 - 2026-07-17

### Approval, evidence and fact integrity
- Added central `ExportEligibilityPolicy` for export, approved-fact creation, and trusted-learning gates.
- Blocked export/fact creation for unapproved batches, unresolved review rows, page errors, failed files, and failed scan pages.
- Kept approved facts immutable and export-event backed, with reconciliation between corrected export rows and fact totals.
- Changed batch deletion endpoint to archive batches instead of physically deleting rows, files, export events, and approved facts.
- Added fact IDs to insight metrics and a fact drill-down endpoint for immutable fact/evidence inspection.
- Prevented unresolved rows from becoming trusted candidate labels, supplier-pattern evidence, or learning recommendation evidence.

## v3.69.11 - 2026-07-15

### Release stabilisation and regression closure
- Closed the Phase 11 regression suite: zero collection errors and zero unexplained test failures in the local full test run.
- Replaced brittle source-slicing and exact-string tests with functional or architecture checks for saved regions, DI/provider evidence, native-text fallback, resolver ordering, and UX contracts.
- Added deterministic release ZIP packaging with an `ApprovLinq/` top-level folder to match the Docker build context.
- Added a PostgreSQL stable-upgrade integration gate alongside the clean-database migration gate.
- Preserved extraction, arbitration, saved regions, totals, BCRS, review, and export behavior while tightening release hygiene.

## v3.61.0 — 2026-03-17

### Supplier name & code improvements
- OpenAI is now always consulted for supplier name extraction (previously only called when the rule-based result looked suspicious). AI result always wins, ensuring the invoice *sender* is captured rather than the *recipient*.
- Supplier-to-posting-account matching upgraded from exact `ilike` to three-level fuzzy matching:
  1. Exact case-insensitive match
  2. Normalised containment — e.g. "BP FUEL CARD" matches list entry "BP"
  3. Word-overlap ≥ 50% — e.g. "Acme Supplies" matches "Acme Supplies Limited"
- When a fuzzy match is found the supplier name is canonicalised to the list entry so all rows for the same supplier are consistent.

### Line items (Lines mode)
- Lines mode now uses a dedicated OpenAI call to extract individual goods/service line items as structured objects (description, quantity, unit price, amount).
- Totals, VAT, subtotals, and discount summary rows are explicitly excluded by the prompt.
- Each extracted line item becomes its own row in the output.
- If the sum of line amounts does not reconcile with the invoice total, affected rows are flagged for review.
- Falls back to the previous rule-based splitter if OpenAI is unavailable.

### Export (Excel)
- Fixed crash when `batch_name` is `None` — filename generation now safely defaults to `"batch"`.
- Fixed crash caused by openpyxl being unable to write Python `UUID` and `Decimal` objects to Excel cells — these are now converted to `str` and `float` respectively before export.
- Internal system columns (`id`, `batch_id`, `tenant_id`, etc.) excluded from the export sheet.
- `supplier_posting_account` and `nominal_account_code` added to the preferred column order.

### Version management
- `VERSION` file introduced at `ApprovLinq/VERSION`.
- `/version` FastAPI endpoint exposes the current version as JSON.
- All pages display a live version badge via `common.js`.
- Pre-commit hook at `.git/hooks/pre-commit` auto-increments the patch number on every commit.
- Helper scripts: `scripts/bump-version.sh` and `scripts/install-hooks.sh` for new clones.

---

## v3.60 and earlier

Pre-changelog. See git history for changes prior to v3.61.0.

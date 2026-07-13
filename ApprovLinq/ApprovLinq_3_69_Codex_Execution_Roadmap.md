# ApprovLinq 3.69.0 — Codex Execution Roadmap

## Purpose

This document converts the technical review roadmap into a sequence of small, testable Codex implementation phases.

Baseline:

- Build: `ApprovLinq_3_saved_rules_modal_row_fix.zip`
- Version: `3.69.0`
- Stable baseline tag recommended: `v3.69-stable-baseline`

## Global guardrails

These instructions apply to every phase:

- Do not hard-code suppliers, invoice numbers, dates, values, layouts or batch-specific assumptions.
- Preserve current working extraction behaviour unless the current phase explicitly changes it.
- Preserve Azure DI, native-text, OCR and OpenAI fallback capability.
- Preserve totals reconciliation and BCRS/deposit safeguards.
- Preserve saved-region governance, saved rules, candidate persistence, review explainability, audit and export.
- Do not allow an LLM or learning process to activate production rules automatically.
- Do not delete or weaken tests merely to obtain a passing result.
- Prefer functional tests over source-string/static implementation assertions.
- Use Alembic for database schema changes.
- Each phase must end with tests, a changed-files report, rollback instructions and a stop.
- Do not begin the next phase in the same Codex task.

---

# Phase 0 — Security and release hygiene

## Objective

Remove packaged secrets and prevent them from returning without changing extraction logic.

## Scope

1. Remove `.env` from tracked/distributed files.
2. Add `.gitignore` and `.dockerignore`.
3. Exclude `.env`, credentials/private keys, local uploads/exports, caches, test databases and temporary provider files.
4. Add `.env.example` containing names only, with no secrets.
5. Add a packaging/static test that fails if secret files are included.
6. Review Docker build context so secrets are not copied into images.
7. Document which credentials must be rotated manually.

## Must not change

- extraction provider order;
- arbitration;
- saved regions;
- totals;
- BCRS;
- review;
- export.

## Acceptance criteria

- Application still starts when environment variables are supplied externally.
- No real credential is present in the repository or distributable archive.
- Existing compile/static checks pass.
- New packaging test passes.

---

# Phase 1 — Stop unsafe automatic learning

## Objective

Prevent unreviewed scan output from teaching future supplier matching.

## Scope

1. Inspect `_learn_supplier_patterns`, `_match_supplier_by_pattern`, supplier-pattern models/tables and review/export outcome paths.
2. Stop automatic activation or merging of supplier patterns after scan completion.
3. A pattern may only become learning evidence after a trusted outcome: explicit row acceptance, manual correction, or approved/exported batch outcome.
4. Store untrusted discoveries as inactive proposals or learning evidence, not active rules.
5. Preserve existing active patterns already in production.
6. Add audit/provenance: source row/run, user/outcome, created time, status, activation/version information.
7. Do not implement the full learning agent in this phase.

## Must not change

- provider extraction;
- totals/BCRS;
- saved-region replay;
- general arbitration;
- export output.

## Acceptance criteria

- Completing a scan cannot create or activate a supplier pattern from an unreviewed row.
- Accepted/corrected/exported outcomes can create trusted learning evidence.
- Existing active patterns still work.
- Tests cover unsafe and trusted learning paths.

---

# Phase 2 — Immutable scan runs and evidence preservation

## Objective

Stop reprocessing from deleting prior candidates, corrections and outcomes.

## Scope

1. Introduce a `scan_run` model with batch, build/version, settings fingerprint, provider configuration fingerprint, status, timestamps and parent/reprocess relationship.
2. Link raw read snapshots, candidates and outcomes to the scan run.
3. Reprocessing creates a new run instead of deleting prior rows/evidence.
4. Preserve previous results for comparison.
5. Define how one run becomes the current/promoted projection.
6. Add migration and rollback instructions.
7. Keep compatibility with existing batches.

## Must not change

- selected values produced by the stable extraction path;
- totals/BCRS;
- saved-region selection;
- export columns.

## Acceptance criteria

- Reprocessing the same batch preserves previous run evidence.
- Candidate and correction history remains available.
- Current run can be identified explicitly.
- Existing review/export flows continue to work.

---

# Phase 3 — Soft block instead of physical deletion

## Objective

Preserve duplicate/false-positive rows and their evidence while blocking export.

## Scope

1. Add a row status or export eligibility model, for example: `active`, `blocked_duplicate`, `blocked_false_positive`, `superseded`.
2. Replace physical delete/block behaviour with status changes.
3. Record reason, user, timestamp and optional note.
4. Preserve candidates, corrections and audit evidence.
5. Update export filters explicitly.
6. Add a safe override/restore path with audit.

## Acceptance criteria

- Blocking a row excludes it from export.
- The row and all evidence remain queryable.
- Restoring/overriding is audited.
- Existing corrected export behaviour remains intact.

---

# Phase 4 — Cross-batch duplicate detection

## Objective

Detect likely duplicates against previously approved/exported transactions.

## Scope

1. Keep current within-batch duplicate detection.
2. Add cross-batch comparison scoped by tenant/company.
3. Use document type, canonical supplier or VAT where reliable, normalised invoice/credit-note number, invoice date, total, currency and document fingerprint as supporting evidence.
4. Do not auto-delete.
5. Flag and block export only where confidence meets a defined threshold.
6. Provide review explanation and override audit.

## Acceptance criteria

- A strong duplicate across batches is flagged.
- Ambiguous matches remain review-only.
- Tenant/company isolation is enforced.
- Blocked duplicates do not export.

---

# Phase 5 — Database migration and test foundation

## Objective

Make schema changes reliable and establish a trustworthy release gate before deeper extraction refactoring.

## Scope

1. Make Alembic authoritative.
2. Remove or isolate production `create_all()` and ad hoc startup `ALTER TABLE` logic.
3. Add a clear schema-version startup check.
4. Resolve case-only column collisions such as `description` / `"Description"`, `quantity` / `"Quantity"`, and `unit_price` / `"UnitPrice"`.
5. Prefer provider-prefixed columns or structured JSON for raw DI fields.
6. Add PostgreSQL integration tests.
7. Create a redacted golden-invoice regression pack.
8. Separate obsolete source-string tests from functional tests.
9. Record baseline supplier, invoice/date, totals, BCRS, duplicate, review-rate and performance metrics.

## Acceptance criteria

- Clean database migration succeeds.
- Upgrade from the stable schema succeeds.
- Production-engine integration tests pass.
- Golden regression baseline is recorded and repeatable.

---

# Phase 6 — Candidate-only saved regions and rules

## Objective

Remove order-dependent row mutation while preserving existing decisions.

## Scope

1. Introduce a typed candidate envelope containing field, value, source, confidence, evidence reference, validation result, page/region, identity score and status/reason.
2. Make DI, native text, OCR, saved regions and rules emit candidates.
3. Saved regions must no longer write selected values directly.
4. Rules must no longer write selected values directly unless explicitly manual/authoritative.
5. Resolver becomes the only selected-value writer.
6. Preserve primary-before-fallback, inactive filtering, dead-region suppression, page scoping, caching and field validation.
7. Keep invoice number/date validation-focused.
8. Compare output against the golden regression pack.

## Acceptance criteria

- Stable baseline output does not regress beyond agreed tolerances.
- Candidate source and rejection reason are visible.
- Saved regions cannot overwrite a different field.
- Rule/region execution order no longer changes final results unexpectedly.

---

# Phase 7 — Resolver separation and maintainability

## Objective

Split the monolithic decision pipeline into explicit services without changing outcomes.

## Scope

Extract services for provider gateway, candidate normalisation, supplier resolver, amount resolver, account/nominal resolver, saved-region service, duplicate resolver and scan-run orchestration.

Preserve hard invariants: totals reconciliation, BCRS/deposit evidence, invoice-number/date validity, tenant/company isolation and duplicate/export blocking.

## Acceptance criteria

- Golden regression output remains equivalent.
- Service boundaries have focused tests.
- Broad exception handling is reduced where safe.
- Existing API/UI contracts remain compatible.

---

# Phase 8 — Durable scan processing

## Objective

Move scan execution out of in-process FastAPI background tasks.

## Scope

1. Add a durable job model/queue.
2. Add atomic job claim/lease.
3. Add heartbeat and stale-job recovery.
4. Add idempotency by scan run/page.
5. Add per-provider/page retry policy.
6. Add cancellation and persisted progress.
7. Web API enqueues and reports status only.
8. Do not change extraction decisions.

## Acceptance criteria

- Restarting the web process does not lose a scan.
- Multiple workers cannot process the same run/page concurrently.
- Failed pages can retry safely.
- Progress survives process restart.

---

# Phase 9 — Controlled learning recommendation agent

## Objective

Introduce agentic improvement as a proposal-and-evaluation service, not autonomous production mutation.

## Scope

The agent may inspect accepted/rejected candidate outcomes, group recurring failures, propose aliases/regions/nominal mappings, identify dead regions, replay proposals, score quality/latency/cost and produce evidence reports.

The agent must not edit production code, activate rules directly, export transactions, bypass tenant/company scope, or override hard invariants.

Add proposal models, evaluation runs, reviewer decisions, canary scope and promotion/rollback state.

## Acceptance criteria

- Every proposal has evidence and replay results.
- Promotion requires approval.
- Promoted changes are versioned and reversible.
- Production scan remains deterministic and bounded.

---

# Phase 10 — Approved invoice fact and expense insights

## Objective

Create trustworthy financial insights from approved/exported data only.

## Scope

1. Create an immutable `approved_invoice_fact` or equivalent.
2. Populate from final approved/exported corrected snapshots.
3. Exclude unresolved reviews, page errors, blocked duplicates, superseded runs and unapproved scans.
4. Include canonical supplier, document type, dates, net/VAT/total, currency/reporting amount, nominal/category, BCRS/deposit, version and evidence reference.
5. Build deterministic monthly spend, supplier concentration, category spend, duplicate exposure, VAT exceptions, credit-note impact and variance insights.
6. Add LLM narratives only over computed structured metrics.

## Acceptance criteria

- Insight totals reconcile to approved/exported facts.
- Every insight supports drill-down.
- LLM text distinguishes facts from hypotheses.
- No metric is calculated from raw OCR text.

---

# Standard Codex planning prompt

```text
Read attached :

ApprovLinq_3_69_Codex_Execution_Roadmap.md and build zip

Focus only on Phase 10 — Approved invoice fact and expense insights.

Inspect the current implementation and prepare a detailed implementation plan.

Do not modify any files yet.

Identify:

- exact files, classes and functions involved;
- database changes and Alembic migrations;
- existing tests protecting current functionality;
- new functional tests required;
- risks to the stable extraction baseline;
- backward compatibility considerations;
- rollback approach;
- dependencies on later phases.

Respect all global guardrails in the roadmap.

Do not implement later phases.
```

# Standard Codex implementation prompt

```text
Read attached :

ApprovLinq_3_69_Codex_Execution_Roadmap.md and build zip

Implement only Phase 10 — Approved invoice fact and expense insights, following the approved plan.

Requirements:

- Do not implement later phases.
- Do not redesign unrelated components.
- Preserve all functionality listed under “Must not change”.
- Do not hard-code suppliers, invoices, dates, values or layouts.
- Use Alembic for schema changes.
- Add functional tests for the phase.
- Do not delete or weaken existing tests merely to pass.
- Run compile/static checks, relevant existing tests, new tests and the golden regression pack where available.

At completion provide:

1. Root cause or design issue addressed.
2. Files changed.
3. Database migrations.
4. Behaviour preserved.
5. Tests run and exact results.
6. Regression comparison.
7. Rollback steps.
8. Known limitations.
9. Stop. Do not begin the next phase.
10. Provide a zipped build of the phase package.
```

# ApprovLinq 3.69.0 — Stable Build Review and Agentic Roadmap

## Review scope

Baseline reviewed: `ApprovLinq_3_saved_rules_modal_row_fix.zip` (`VERSION` = `3.69.0`).

The review covered:

- batch processing and provider selection;
- Azure Document Intelligence, native-text, OCR and OpenAI paths;
- supplier resolution, saved rules, saved regions and arbitration;
- candidate persistence and user outcome labelling;
- duplicate handling and corrected export;
- analytics endpoints;
- database/schema lifecycle, background processing and deployment packaging;
- existing automated tests and static checks.

No extraction logic was changed during this review.

---

## Executive conclusion

This is a materially stronger build than the earlier versions. It already contains several foundations required for a learning invoice system:

- DI-first structured extraction when Azure DI is configured;
- native-text and OCR fallback paths;
- field candidates with source, confidence and evidence;
- supplier identity resolution;
- saved-region governance, primary/fallback regions and page scoping;
- totals reconciliation and BCRS safeguards;
- candidate outcome labels after review/export;
- review audit and versioned export events;
- performance instrumentation.

However, it is **not yet safely agentic**. It currently has a deterministic extraction and arbitration pipeline with some automatic learning. The most important concern is that supplier patterns are learned automatically from scan output before the batch has necessarily been reviewed or approved. This can cause the system to reinforce a wrong supplier decision in later batches.

The immediate recommendation is to **freeze the working extraction behaviour**, correct the learning/data-lifecycle issues first, and introduce an offline, evaluated learning loop rather than letting an LLM or pattern learner change production decisions directly.

---

## Current effective scan flow

The effective non-baseline flow in `app/routers/batches.py::_process_batch_job` is:

1. Delete existing extracted rows for the batch.
2. Process each PDF page.
3. In `process_pdf_page`:
   - extract native PDF text for possible fallback/evidence;
   - attempt Azure DI first when configured and not circuit-broken;
   - optionally use Azure DI Read fallback;
   - use native text if useful;
   - use OCR fallback when required;
   - run deterministic extraction;
   - merge structured DI fields;
   - optionally use OpenAI vision/text and validation;
   - remediate and validate financial fields.
4. Before row creation, optionally replace the extracted supplier using a stored supplier keyword pattern.
5. Persist the raw read snapshot.
6. Resolve supplier identity.
7. Apply supplier/master/history enrichment.
8. Replay saved regions, which can directly mutate the row.
9. Re-run master enrichment if the supplier changed.
10. Apply saved rules, which can directly mutate the row.
11. Run invoice arbitration.
12. Persist selected and rejected candidates.
13. Apply BCRS/deposit row logic.
14. Commit the page.
15. Detect duplicates within the current batch.
16. Mark batch complete.
17. Automatically learn/merge supplier patterns from the completed scan rows.

### Provider order finding

Azure DI is already attempted before OCR in the active flow. Native text is extracted early but is not used as the main field source when DI succeeds. This is appropriate. Documentation and tests are not fully aligned with the implemented order.

---

# Priority findings

## P0 — Security: secrets are packaged in the build

The uploaded archive contains a `.env` file with non-empty values for database and cloud-provider credentials. No `.gitignore` or `.dockerignore` is present, and the Dockerfile copies the whole application directory into the image.

### Risk

- Credentials can be distributed in zips, source control, backups or container layers.
- Even if these credentials are test credentials, their presence establishes an unsafe release process.

### Required fix

1. Rotate the database, Azure DI, OCR and OpenAI credentials as a precaution.
2. Remove `.env` from distributable archives and source control.
3. Add `.gitignore` and `.dockerignore` entries for `.env`, local uploads, exports, caches and test databases.
4. Supply secrets only at runtime through deployment environment variables or a secret manager.
5. Add a CI secret scan and a packaging test that fails if `.env` or private keys are included.

---

## P0 — Learning safety: unreviewed scans can teach future supplier matching

`app/routers/batches.py::_learn_supplier_patterns` runs automatically after batch processing. It learns from rows that have a supplier name matching an active supplier master and a header. It does **not** require the row to have been manually accepted, approved or exported.

The learned pattern is then used before supplier resolution on future scans by `_match_supplier_by_pattern`, where it can replace the initial provider result.

### Risk

A wrong supplier decision that happens to canonicalise to a master supplier can be converted into a persistent pattern. Future invoices may then be prefilled with the same wrong supplier, creating self-reinforcing errors.

### Required fix

- Stop promoting supplier patterns directly from scan completion.
- Create learning evidence only from one of these trusted outcomes:
  - explicit row acceptance;
  - a manual correction;
  - approved/exported batch outcome;
  - repeated high-confidence agreement between independent sources, placed in quarantine until evaluated.
- Store a proposed pattern separately from an active pattern.
- Evaluate a proposal against historical labelled invoices before promotion.
- Require an approval threshold and provide rollback/versioning.

This is the highest-priority functional change for the planned “keeps improving” capability.

---

## P1 — Reprocessing deletes rows and learning evidence

At the start of `_process_batch_job`, all existing `InvoiceRow` records for the batch are deleted. Row corrections and field candidates use cascading foreign keys, so reprocessing can destroy correction and outcome history.

### Risk

- Manual corrections and candidate labels can be lost.
- The system cannot compare extraction version A with version B reliably.
- Negative examples disappear when rows are deleted.
- Reprocessing is not reproducible or auditable.

### Required fix

Introduce scan runs and immutable versions:

- `scan_run` with extractor build, settings fingerprint, provider versions and timestamps;
- immutable `read_snapshot` and `candidate_event` records linked to the scan run;
- current row/result as a projection, not the only historical record;
- reprocessing creates a new run rather than deleting the prior run;
- allow side-by-side comparison and explicit promotion of a run to “current”.

---

## P1 — Delete/block export physically deletes the row

The review delete endpoint records an audit event and then physically deletes the invoice row. This blocks export, but it also cascades deletion of candidate evidence and corrections.

### Required fix

Use soft state instead:

- `export_eligible` or `row_status` (`active`, `blocked_duplicate`, `blocked_false_positive`, `superseded`);
- block reason, user and timestamp;
- preserve candidates and mark the rejected row as a valuable negative outcome;
- filter export explicitly by approved/export-eligible state.

---

## P1 — Duplicate detection is limited to the current batch

`_mark_duplicate_invoice_rows` only compares rows in the same batch. It uses invoice number, date and total, with a one-cent tolerance, which is sensible for within-batch detection.

### Required improvement

Add a cross-batch duplicate index against approved/exported invoices for the same tenant/company. Use:

- document type;
- canonical supplier or supplier VAT when reliable;
- normalised invoice/credit-note number;
- invoice date;
- total and currency;
- document fingerprint as supporting evidence.

Do not auto-delete. Flag and block export until resolved when confidence is high.

---

## P1 — Background processing is not durable or multi-instance safe

Processing uses FastAPI `BackgroundTasks` and an in-memory `_ACTIVE_BATCHES` set.

### Risk

- A process restart interrupts the job.
- Each web worker has its own active-batch lock.
- Multiple instances can process the same batch.
- Progress and retries are not durable.

### Required fix

Move scanning to a durable worker/queue with:

- database-backed job state;
- atomic claim/lease;
- heartbeat and stale-job recovery;
- idempotency key per scan run;
- retry policy per provider/page;
- cancellation support;
- progress events.

The web API should enqueue and report status, not execute document processing in the web process.

---

## P1 — Saved regions are candidates in concept but still mutate rows before arbitration

Saved-region replay has strong governance features: active/archived/deleted filtering, primary-first order, dead-hint suppression, page scoping and read caching. However, `_apply_remap_hints` still directly changes row fields before arbitration.

Saved rules also directly change rows before arbitration.

### Risk

- The original provider value and the region/rule proposal are not cleanly separated at decision time.
- Arbitration may compare against a row that has already been modified.
- Explanations become harder to reason about.
- Multiple overlapping decision systems can produce order-dependent results.

### Required improvement

Use one typed candidate envelope for every source:

- value;
- field;
- source/provider;
- confidence;
- evidence reference;
- validation result;
- supplier/template identity score;
- page/region information;
- candidate status.

Saved regions and rules should emit candidates. The resolver should be the only component that writes the selected field value.

Manual overrides remain the highest-priority explicit decision.

---

## P1 — Current analytics are not yet safe for expense insights

The analytics endpoints aggregate directly from all `InvoiceRow` records for a tenant/company. They do not consistently restrict the data to reviewed, approved or exported results, and do not explicitly exclude duplicates, page errors or blocked rows. They also read raw row values rather than a versioned corrected/exported fact.

### Risk

Spend totals and supplier rankings can be materially wrong.

### Required fix before expanding insights

Create an approved transaction fact model sourced from the final corrected/exported snapshot. Include:

- tenant/company;
- immutable export/run version;
- canonical supplier and supplier VAT/account;
- document type and status;
- invoice date/due date;
- net, VAT, total, currency and reporting-currency amount;
- nominal account/category;
- BCRS/deposit classification;
- duplicate/block status;
- evidence/confidence and drill-down reference.

All financial insight endpoints should query this curated fact, not live extraction rows.

---

## P1 — Database schema and migration lifecycle need consolidation

The application calls `Base.metadata.create_all()` during import and also runs a large list of startup `ALTER TABLE` statements, while Alembic is present.

There are also case-differing columns in `invoice_read_details`, such as `description` and quoted `"Description"`, `quantity` and `"Quantity"`, and `unit_price` and `"UnitPrice"`. PostgreSQL can distinguish quoted mixed-case names, but SQLite cannot, causing schema creation failures in the test environment and creating long-term maintenance risk.

### Required fix

- Make Alembic the authoritative schema mechanism.
- Remove non-fatal `create_all` and ad hoc startup schema mutation from production startup.
- Add a startup schema-version check that fails clearly when migrations are missing.
- Normalise DI raw fields into JSON or prefix raw provider columns (`di_description`, `di_quantity`, etc.) rather than using case-only column distinctions.

---

## P1 — The automated test suite is not a reliable release gate yet

### Checks run

- `python -m compileall app` — passed.
- `node --check app/static/js/review.js` — passed.
- `node --check app/static/js/rules.js` — passed.

A broad pytest run using SQLite and disabled external providers produced:

- 856 passed;
- 8 skipped;
- 80 failed;
- 42 errors.

Many failures are stale static assertions tied to old function text, comments or UI markup. A modern targeted suite produced:

- 123 passed;
- 11 failed.

The remaining targeted failures include test/code drift around preflight behaviour, provider fallback expectations, exact saved-region implementation strings and function-order assertions. Some are clearly brittle static tests, but they still show that the tests and the documented contract have diverged.

### Required fix

Build a tiered release gate:

1. **Unit tests** for parsers, validators and resolvers.
2. **Database integration tests** against the production database engine.
3. **Golden invoice regression tests** using redacted representative PDFs and approved expected candidates/final results.
4. **Provider contract tests** with recorded/sanitised responses.
5. **End-to-end review/export tests**.
6. **Performance and cost budgets**.

Retire obsolete static string tests instead of weakening functional assertions.

---

## P2 — Maintainability and operational issues

### Monolithic modules

The main extraction and orchestration files are very large:

- `app/services/extractor.py`: approximately 5,500 lines;
- `app/routers/batches.py`: approximately 5,200 lines;
- `app/routers/review.py`: approximately 2,800 lines.

There are many broad `except Exception` paths. This makes behavioural ordering difficult to verify and can hide regressions.

Split into explicit services:

- provider gateway;
- candidate normalisation;
- supplier resolver;
- amount resolver;
- account resolver;
- saved-region service;
- duplicate service;
- scan-run orchestrator;
- learning/evaluation service.

### Provider/LLM call overlap

The deterministic extractor can use OpenAI for line descriptions and candidate ranking, while DI and later OpenAI fallback/validation may also run. Introduce a per-page call plan and cost budget so the same page does not receive unnecessary overlapping LLM calls.

### Documentation drift

- Build version is 3.69.0, while the changelog stops at 3.61.0.
- README still describes native-text-first extraction, while the code is DI-first when configured.
- Batch comments describe a real network preflight, but `preflight.py` explicitly performs configuration-only checks.

Update documentation as part of every release gate.

### Sensitive source data retention

Raw page text, provider payloads and invoice images are valuable for audit and learning but may contain personal and financial data. Define:

- tenant-level provider/privacy policy;
- encryption and access controls;
- retention/deletion schedule;
- redaction for evaluation datasets;
- audit of external-provider transmission;
- controls preventing raw invoice instructions from influencing LLM behaviour.

---

# Recommended stable flow

The following flow retains current working extraction while removing order-dependent mutations:

1. **Create immutable scan run**
   - settings fingerprint, build version and provider policy.
2. **Acquire document/page**
   - orientation, quality and document fingerprint.
3. **Collect provider evidence**
   - Azure DI first when available;
   - native text and OCR only as required;
   - targeted region reads;
   - optional LLM only for unresolved ambiguity.
4. **Persist immutable raw snapshot and all candidates**
   - before any business decision.
5. **Normalise and validate candidates**
   - field-specific validation, no row mutation.
6. **Supplier identity resolver**
   - DI/OCR/native/header region/VAT/master/accepted aliases.
7. **Template and saved-region candidate generation**
   - primary then fallback, current page scope, identity safeguards.
8. **Amount resolver**
   - labelled summaries and reconciliation-first selection.
9. **Account/nominal resolver**
   - trusted rules, supplier defaults and accepted history.
10. **Invoice/date/currency validation**
    - light validation, no historical replacement of dynamic values.
11. **Duplicate resolver**
    - current batch and prior approved/exported facts.
12. **Write selected projection and explainability**
    - resolver is the only writer of selected values.
13. **Human review where required**
    - correction, acceptance, block or override.
14. **Create immutable approved/export snapshot**
    - source for accounting export and analytics.
15. **Learning job creates proposals**
    - only from trusted outcomes.
16. **Offline replay/evaluation, approval, canary and promotion**
    - with rollback.

---

# Agentic and continuous-learning architecture

## What “agentic” should mean here

The production scan path should not be a free-running LLM that changes its own rules. A safe agent should be a controlled improvement worker with tools, evidence, evaluation and approval gates.

### Agent tools

The learning agent may:

- inspect labelled candidate outcomes;
- group recurring supplier/layout failures;
- propose a supplier alias, stable anchor, saved region or account rule;
- detect dead/conflicting saved regions;
- replay a proposal against historical labelled invoices;
- compare precision, recall, false-overwrite rate, review rate, latency and cost;
- produce an explanation and evidence pack;
- recommend promote, reject, merge or retire.

It should **not** directly edit production rules or code.

## Safe improvement loop

```text
Scan evidence
  → deterministic candidates/resolvers
  → human acceptance or correction
  → immutable outcome event
  → learning agent proposes a change
  → offline historical replay
  → policy/evaluation gate
  → human approval or tightly bounded auto-approval
  → tenant/company canary
  → promotion with version and rollback
```

## Promotion policy

A proposal should be promoted only when it:

- improves the target field on a minimum labelled sample;
- does not reduce protected-field accuracy;
- causes no new unsafe overwrites;
- preserves totals/BCRS/export invariants;
- stays within latency and cost budgets;
- is scoped to the correct tenant/company/supplier/template;
- has a rollback version.

## Recommended learning phases

### Phase A — Trustworthy learning data

- immutable scan runs;
- candidate/outcome event store independent of row deletion;
- reviewed/exported labels;
- soft-blocked duplicates as negative labels;
- golden regression corpus.

### Phase B — Recommendation agent

- agent proposes aliases, regions and nominal mappings;
- no automatic production changes;
- replay report and reviewer approval.

### Phase C — Bounded auto-promotion

Allow automatic promotion only for low-risk, highly evidenced proposals, for example retiring a saved region that has repeatedly returned blank and has a proven replacement. Supplier identity and monetary decisions should remain more tightly governed.

### Phase D — Learned candidate ranking

Train or configure a field-specific ranker using candidate features and accepted outcomes. Keep hard invariants outside the model:

- totals reconciliation;
- date and invoice-number validity;
- tenant/company isolation;
- duplicate/export blocking;
- BCRS/deposit evidence requirements.

### Phase E — Agentic exception handling

For low-confidence rows, an agent can choose bounded tools such as:

- request one targeted crop;
- compare two provider candidates;
- query approved supplier aliases;
- run totals reconciliation;
- explain why review is required.

It must have a maximum step count, call budget and no authority to export or promote rules.

---

# Expense insights roadmap

## Foundation first

Build an `approved_invoice_fact` or equivalent immutable view/table from exported/approved corrected data.

## Initial deterministic insights

1. Monthly net/VAT/gross spend.
2. Spend by canonical supplier, nominal account and category.
3. Supplier concentration and dependency.
4. Duplicate and repeated-invoice exposure.
5. VAT exceptions and unreconciled totals.
6. Credit notes and net spend.
7. Average invoice value and invoice frequency.
8. New or inactive suppliers.
9. Spend variance versus prior month/quarter/year.
10. Late or unusual invoices when due-date data becomes reliable.

## Later LLM narrative layer

Use the LLM to explain already-computed metrics, not to calculate the financial totals from raw invoice text. Every narrative should link to the structured query and drill-down rows, distinguish facts from hypotheses, and avoid giving accounting conclusions where the underlying data is incomplete.

Potential examples:

- “Spend with Supplier A increased 31% month-on-month, mainly in nominal account X.”
- “Three likely duplicates worth €Y remain blocked from export.”
- “VAT reconciliation exceptions rose from 2 to 8 invoices this month.”
- “Supplier concentration is increasing; the top three suppliers represent Z% of approved spend.”

---

# Recommended implementation sequence

## Release 1 — Stabilise the foundation

1. Remove/rotate packaged secrets and add packaging controls.
2. Disable unreviewed supplier-pattern promotion.
3. Add immutable scan-run and candidate/outcome history.
4. Replace physical row deletion with soft block status.
5. Add cross-batch duplicate checking.
6. Make Alembic authoritative and resolve case-only schema columns.
7. Establish a production-engine integration and golden-invoice release gate.

## Release 2 — Simplify decision flow

1. Introduce typed candidate envelopes.
2. Make regions/rules candidate-only.
3. Make resolvers the only selected-value writers.
4. Remove or formalise the separate provider-baseline mode.
5. Add LLM/provider call budget and full provenance.
6. Split the monolithic modules.

## Release 3 — Controlled learning agent

1. Proposal tables and versioned policy objects.
2. Offline replay evaluator.
3. Reviewer proposal UI.
4. Canary/promotion/rollback workflow.
5. Field-level quality, review-rate, latency and cost dashboards.

## Release 4 — Expense intelligence

1. Approved/exported invoice fact model.
2. Deterministic insight APIs and drill-down dashboards.
3. Anomaly detection on approved facts.
4. LLM narrative summaries over structured metrics.

---

## Final assessment

The current build is suitable as a **functional baseline for further controlled testing**, but it should not yet be allowed to learn autonomously from all completed scans or serve authoritative expense analytics.

The extraction logic should remain frozen until a golden regression corpus is in place. The next engineering work should target security, immutable evidence, trusted learning outcomes, durable processing and a single candidate-to-resolver decision flow. Those changes will make later agentic behaviour safer and significantly easier to evaluate without breaking the totals, regions, rules and review functionality that now works well.

# ApprovLinq 3.69.0 — Ten-Phase Implementation Review

## Executive conclusion

The package shows substantial, structured progress across the roadmap, but it is **not yet safe to approve as a completed production release**.

The phase-specific acceptance tests are mostly present and largely pass, but the complete regression suite does not. More importantly, several roadmap guarantees are only partially enforced in the implementation:

- Phase 10 facts can be created from active rows that are still unresolved or review-required.
- Purportedly immutable evidence can still be physically deleted through batch deletion and cascading foreign keys.
- The durable worker has leasing but no reliable heartbeat/fencing design, allowing a stale lease to cause duplicate concurrent processing.
- Phase 7 introduces service façades, but much of the core business logic remains inside very large router modules.
- Phase 9 governance lacks role-level permissions, separation of duties and enforced canary controls.

The recommended approach is to **freeze further feature development and run a release-stabilisation phase first**.

---

## Review scope

Reviewed:

- `ApprovLinq_3_69_Codex_Execution_Roadmap(1).md`
- `ApprovLinq_3_69_phase10_approved_invoice_fact_expense_insights(1).zip`
- application source, Alembic migrations, tests, packaging controls and Docker configuration
- compile/static validation
- full pytest collection and regression suite
- phase-specific acceptance tests

The archive contains approximately 46,500 lines of Python. The largest remaining modules include:

| File | Approximate size |
|---|---:|
| `app/services/extractor.py` | 5,531 lines |
| `app/routers/batches.py` | 5,458 lines |
| `app/routers/review.py` | 3,047 lines |
| `app/services/invoice_arbitration.py` | 1,131 lines |

---

## Validation results

### Compile check

Command:

```bash
python -m compileall -q app alembic scripts tests
```

Result:

```text
PASS — COMPILE_OK
```

### Full test suite

The unmodified full suite stops during collection because `tests/test_region_text.py` extracts a source-code slice using a marker that no longer exists. It consequently executes router decorators without a defined `router` variable and raises:

```text
NameError: name 'router' is not defined
```

After excluding that obsolete/broken collection test:

```text
80 failed, 941 passed, 9 skipped, 246 warnings
```

This is the most important release finding. A build with 80 unexplained regressions cannot be treated as a completed ten-phase implementation.

### Phase-specific acceptance suite

The selected Phase 0–10 tests produced:

```text
2 failed, 63 passed, 1 skipped
```

Both failures are in Phase 0 release-hygiene tests:

1. Runtime import/test activity creates `invoice_scanner.db` inside the source tree.
2. The packaging test expects a distributable ZIP inside the extracted project tree rather than testing the actual externally built artifact.

The PostgreSQL migration/integration test was skipped because no external PostgreSQL test environment was configured.

---

# Phase-by-phase assessment

## Phase 0 — Security and release hygiene

**Verdict: Partial; release gate fails.**

### Correctly implemented

- `.env` is not packaged.
- `.env.example` contains environment-variable names without real secrets.
- `.gitignore` and `.dockerignore` cover common credentials, local databases, uploads, exports, caches and temporary files.
- The actual uploaded archive did not contain an obvious credential or private-key file.

### Remaining issues

- The ZIP uses Windows backslashes in member names. Standard Linux `unzip` emits a warning and exits non-zero even though it extracts the files. This is a portability defect for CI/CD and Linux deployment.
- `Dockerfile` uses paths such as `COPY ApprovLinq/requirements.txt ...`, but the delivered archive is rooted directly at `requirements.txt`, `app/`, and so on. Unless the caller supplies a specially arranged parent build context, the delivered package will not build as-is.
- The packaging test is coupled to an archive being stored inside the source tree rather than creating and validating a release artifact in a staging directory.
- Application/test imports can create `invoice_scanner.db` in the repository.
- Apt packages are installed more than once in the Dockerfile.

### Required improvement

Create a deterministic release command that:

1. copies only approved files to a clean staging directory;
2. creates ZIP entries using POSIX separators;
3. runs `unzip -t` on the built artifact;
4. scans the final artifact for secrets and runtime files;
5. builds the Docker image from that exact artifact/context;
6. starts the container and runs a smoke test.

---

## Phase 1 — Stop unsafe automatic learning

**Verdict: Mostly implemented, but trust can still be granted too early through export.**

### Correctly implemented

- New supplier discoveries are separated from existing active supplier patterns.
- Proposal/evidence status and provenance are present.
- Existing active patterns remain usable.

### Remaining issue

The export path can export active rows without enforcing that all review-required or page-error states have been resolved. Export also participates in candidate labelling and supplier-pattern promotion. Therefore an unresolved row can potentially become trusted evidence merely because the batch was exported.

### Required improvement

Introduce one backend `ExportEligibilityPolicy` used by:

- corrected export;
- fact materialisation;
- trusted candidate labelling;
- supplier-learning evidence promotion.

It must reject rows or batches containing unresolved review, page errors, unapproved state, blocked/superseded rows, or non-current scan runs.

---

## Phase 2 — Immutable scan runs and evidence preservation

**Verdict: Partial.**

### Correctly implemented

- Reprocessing creates identifiable scan runs.
- Current/promoted run handling exists.
- Candidate and outcome data can be associated with scan runs.

### Remaining issues

- `DELETE /batches/...` still physically deletes rows, corrections, duplicate evidence, audit records, export events, files and the batch.
- `ApprovedInvoiceFact` foreign keys use cascading deletion for batch and source row references.
- File-retention cleanup can remove source PDFs and exports while database evidence remains, breaking later drill-down.

This contradicts the intended immutable-evidence model.

### Required improvement

- Replace physical batch deletion with archival/tombstone status for normal use.
- Restrict physical purge to a privileged retention workflow with a legal/audit policy.
- Make approved facts survive operational-row or batch removal.
- Store a copied evidence snapshot, content hash, provider/result identifiers and durable object-storage reference.
- Add explicit retention states and purge audit.

---

## Phase 3 — Soft block instead of physical deletion

**Verdict: Row-level behaviour is mostly correct; batch-level deletion remains destructive.**

### Correctly implemented

- Duplicate/false-positive rows have status-based export blocking.
- Restore/override paths and audit fields are present.
- Evidence can remain attached to a blocked row.

### Remaining issue

The row-level soft-block guarantee is undermined by the batch delete path, which can still physically delete all evidence.

### Required improvement

Apply the same preservation model at batch, run, page and row level. A user-facing “delete” should normally archive, not purge.

---

## Phase 4 — Cross-batch duplicate detection

**Verdict: Functionally present, but needs scaling and evidence-source improvements.**

### Correctly implemented

- Tenant/company scoping is represented.
- Strong and ambiguous matches have differentiated outcomes.
- Detection does not simply delete a row.
- Review and override evidence exists.

### Remaining issues

- Candidate history is loaded in batches and compared in Python, producing an approximate O(current rows × candidate rows) process.
- The detector uses mutable operational rows from approved/exported batches instead of immutable approved facts.
- Historical detector decisions may be updated rather than fully versioned with detector model/version and threshold.

### Required improvement

- Use `approved_invoice_fact` as the historical search base.
- Add indexed blocking keys such as tenant/company, canonical supplier/VAT, normalised number, total band, date window and document fingerprint.
- Perform SQL pre-filtering before detailed scoring.
- Store detector version, threshold configuration, supporting features and decision snapshot immutably.

---

## Phase 5 — Database migration and test foundation

**Verdict: Partial; the required release foundation is not yet proven.**

### Correctly implemented

- Alembic migrations exist for the phased schema changes.
- A schema-head check is present for PostgreSQL startup.
- A golden-regression mechanism exists.
- New functional tests exist alongside older source-string tests.

### Remaining issues

- PostgreSQL integration testing is optional and was skipped in this review.
- There is no demonstrated CI gate proving both:
  - migration from an empty database to head; and
  - upgrade from a real 3.69 stable-schema snapshot to head.
- SQLite behaviour masks PostgreSQL-specific schema and transaction risks.
- Some BigInteger primary-key definitions do not use the project’s SQLite-compatible PK variant, causing test insertion failures.
- The suite still contains brittle source slicing and source-string assertions.
- The full regression suite is not green.

### Required improvement

Use PostgreSQL as a mandatory CI service and run:

1. clean database → Alembic head;
2. stable schema/data snapshot → Alembic head;
3. application smoke tests at head;
4. downgrade or database-restore rehearsal;
5. full functional and golden regression suites.

A migration phase cannot be considered complete while this test is skipped.

---

## Phase 6 — Candidate-only saved regions and rules

**Verdict: Mostly implemented on the main scan path, but the invariant is not structural.**

### Correctly implemented

- Typed candidate payloads and provenance are present.
- Main scan calls route saved-region/rule findings into candidate resolution.
- Candidate rejection/explanation support exists.
- Invoice number/date remain validation-sensitive.

### Remaining issue

Legacy branches in saved-rule and remap-hint functions still call `setattr(row, ...)` directly when an optional candidate payload is not supplied. The production path may currently supply the payload, but the API still permits a future caller to bypass the resolver.

### Required improvement

- Remove the optional direct-write branch.
- Make candidate emission mandatory for scan-derived values.
- Create a separate explicit manual-authoritative correction API for user changes.
- Add an architecture test that fails if scan services mutate selected fields outside the resolver/correction service.

---

## Phase 7 — Resolver separation and maintainability

**Verdict: Not fully complete; much of the new service layer is façade delegation.**

### Correctly implemented

- Named service boundaries now exist for supplier, amount, nominal, duplicate and saved-region concerns.
- Focused tests exist for these service entry points.

### Remaining issues

Several services import and call private functions from `app.routers.batches`. This reverses the intended dependency direction: business services depend on HTTP/router modules rather than routers depending on services.

The core router and extractor files remain very large, indicating that the monolithic decision pipeline was not truly separated.

### Required improvement

- Move implementation code and domain dependencies into service/domain modules.
- Restrict routers to authentication, request validation, transaction boundary selection and response mapping.
- Add an architecture test prohibiting `app.services.*` from importing `app.routers.*`.
- Break the extraction orchestration into provider, candidate, resolution, persistence and audit components with typed interfaces.

---

## Phase 8 — Durable scan processing

**Verdict: The queue is durable, but concurrency safety is not production-ready.**

### Correctly implemented

- Jobs and page states are persisted.
- Atomic job claim/lease concepts exist.
- Retry and stale-job recovery concepts exist.
- A separate worker script exists.
- Progress survives a web request ending.

### Critical remaining issues

- A worker claims a lease, but there is no reliable heartbeat throughout a long extraction.
- When the lease expires, stale-job recovery can requeue the same job while the original worker continues processing.
- Page transition to running is not protected with a fencing token or atomic compare-and-set claim.
- Cancellation is not consistently checked inside the page-processing loop.
- State writes do not prove that the writer still owns the current lease.
- The Docker container starts the worker as a background shell process beside Uvicorn, which is not a robust process-supervision or horizontal-scaling model. Each web replica may start another worker.

### Required improvement

Implement:

- periodic heartbeat independent of page completion;
- monotonically increasing fencing/lease token;
- lease-token check on every job/page state mutation;
- atomic page claim;
- cancellation checks between provider calls and before persistence;
- idempotent output keys for run/page/provider attempt;
- separate worker deployment/container;
- chaos tests for worker kill, lease expiry, retry and duplicate-worker contention.

---

## Phase 9 — Controlled learning recommendation agent

**Verdict: Proposal workflow exists, but governance is insufficient.**

### Correctly implemented

- Proposal, evaluation, review, promotion and rollback models/flows exist.
- Evidence and replay concepts are represented.
- Direct autonomous code modification is not implemented.

### Remaining issues

- Any authenticated tenant user can access important learning operations unless an external layer not present in this package prevents it.
- The same user can potentially generate, approve and promote a proposal.
- `canary_scope_json` is recorded but not fully enforced as a bounded production activation.
- No explicit role matrix, risk tier, two-person approval or expiry is enforced.
- Concurrent review/promotion does not clearly use optimistic locking/idempotent transition protection.

### Required improvement

- Add explicit permissions: view, propose, evaluate, approve, promote, rollback.
- Enforce separation of duties for medium/high-risk changes.
- Require two-person approval for production activation.
- Enforce canary tenant/supplier/field/time boundaries in the runtime resolver.
- Add automatic expiry, rollback threshold and immutable transition history.
- Use state-version checks to prevent duplicate/concurrent promotion.

---

## Phase 10 — Approved invoice fact and expense insights

**Verdict: Partial and currently violates the central eligibility guarantee.**

### Correctly implemented

- An approved-invoice fact model and migration exist.
- Facts include supplier/document/amount/category/BCRS and evidence-related fields.
- Structured deterministic insight calculations exist.
- Raw OCR text is not directly used as the metric source.
- Fact versions and fingerprints are represented.

### Critical correctness issue

`materialise_approved_invoice_facts_for_export()` filters active/current rows, but it does not reliably exclude:

- unresolved `review_required` rows;
- page-error rows;
- an unapproved batch;
- rows lacking a final trusted correction/approval outcome.

The export route itself does not enforce a complete approval gate. `approved_summary()` also hard-codes `needs_review` to zero, which can mask the eligibility problem rather than prove it.

This directly conflicts with the Phase 10 objective of deriving facts only from approved/exported corrected snapshots.

### Immutability issue

`ApprovedInvoiceFact` references to batch and source row use cascading deletion. The supposedly immutable fact can therefore disappear when its source operational records are deleted.

### Analytics limitations

- Reporting amounts currently copy source amounts; no FX conversion table, rate date, rate source or provenance exists.
- BCRS/deposit classification uses broad description matching and may classify the whole invoice total as deposit-related.
- Month windows use an approximate number of days rather than exact calendar arithmetic.
- Several analytics load facts into Python rather than aggregating in SQL, which will not scale well.
- Insight responses do not yet provide complete fact/evidence drill-down endpoints.
- LLM narrative generation is not implemented; this is acceptable only if Phase 10 is described as deterministic insights complete and narrative generation deferred.
- Database-level append-only enforcement and uniqueness/idempotency constraints are limited.

### Required improvement

1. Enforce a central export/fact eligibility policy.
2. Reject export/fact materialisation while any in-scope row is unresolved, page-error, blocked, superseded or from a non-current run.
3. Require an approved batch outcome or an explicit approved export event.
4. Make facts append-only and independent of operational-row deletion.
5. Store an immutable corrected snapshot and evidence hash/reference.
6. Add fact and insight drill-down APIs.
7. Add exact reconciliation tests from export rows to facts to metrics.
8. Add FX-rate provenance before labelling converted values as reporting amounts.
9. Calculate BCRS/deposit metrics from explicit split/evidence fields, not broad description regex alone.
10. Move high-volume aggregations into indexed SQL queries/materialised views.

---

# Material behavioural regressions found in the full suite

Although a portion of the 80 failures comes from obsolete source-string tests, several failures appear behaviourally important and must be classified rather than dismissed:

- item descriptions containing “deposit” can falsely trigger BCRS;
- summary-context BCRS classification differs from expected behaviour;
- a VAT regression produced a different value from the established expected result;
- a BCRS decision changed from review-suggested split to automatic split;
- a formerly public `deposit_component` interface is missing/replaced by a private helper;
- auto-approval behaviour differs from earlier expectations;
- nominal validation no longer raises in a previously protected case;
- saved-region behaviour has multiple failing tests;
- preflight configured-state tests fail;
- runtime/database test isolation creates repository artefacts.

Each failure must be assigned one of four outcomes with evidence:

1. genuine regression — fix code;
2. intentional behavioural change — update specification and functional test;
3. obsolete implementation-coupled test — replace with behavioural test;
4. invalid test fixture/environment — repair isolation.

No failing test should simply be removed to obtain a green build.

---

# Recommended next phases

## Phase 11 — Release stabilisation and regression closure

This should be the immediate next phase.

### Scope

- Freeze feature work.
- Classify and close all 80 test failures and the collection error.
- Replace brittle source-slicing/string tests with functional or architecture tests.
- Make PostgreSQL clean-migration and stable-upgrade tests mandatory.
- Fix deterministic packaging and Docker build context.
- Run secret, dependency and container scans on the final artifact.
- Produce a versioned release manifest and changelog.

### Exit gate

- zero collection errors;
- zero unexplained test failures;
- PostgreSQL clean and stable-upgrade paths pass;
- golden extraction comparison approved;
- ZIP, Docker build and startup smoke tests pass.

## Phase 12 — Approval, evidence and fact integrity

### Scope

- central `ExportEligibilityPolicy`;
- mandatory unresolved/page-error/approval checks;
- immutable/export-event-backed fact creation;
- archive instead of physical batch deletion;
- durable evidence retention and drill-down;
- reconciliation from corrected export → fact snapshot → insights;
- prevent unresolved rows from becoming trusted learning evidence.

### Exit gate

- unresolved or errored rows cannot export or create facts;
- approved facts survive operational archival;
- every metric drills down to immutable facts and evidence;
- export, fact and insight totals reconcile exactly.

## Phase 13 — Worker concurrency and operational resilience

### Scope

- heartbeat, fencing token and atomic page claim;
- cancellation checkpoints;
- idempotent page/provider attempts;
- stale-worker and duplicate-worker chaos tests;
- separate worker service/container;
- provider telemetry including Azure operation ID, request ID, status, latency, retries and timeout reason.

### Exit gate

- no page can be committed by an expired lease owner;
- restart/lease-expiry tests do not duplicate processing;
- cancellation and retries are deterministic;
- Azure DI timeout root cause is visible from persisted telemetry.

## Phase 14 — Governance and analytics hardening

### Scope

- learning-agent RBAC and separation of duties;
- enforced canary and rollback controls;
- FX-rate model and provenance;
- exact calendar intervals;
- SQL-based scalable analytics;
- explicit BCRS/deposit fact components;
- narrative facts-versus-hypotheses contract.

---

# Proposed production release gate

Do not promote the package until all of the following are true:

| Gate | Required result |
|---|---|
| Compilation | Pass |
| Test collection | 0 errors |
| Full regression suite | 0 unexplained failures |
| Golden invoice pack | Within explicitly approved tolerances |
| PostgreSQL empty migration | Pass |
| PostgreSQL stable upgrade | Pass |
| Export eligibility | Unresolved/error/blocked/non-current rows rejected |
| Fact reconciliation | Corrected export = fact totals = insight totals |
| Worker chaos test | No duplicate page commit after restart or lease expiry |
| Artifact validation | ZIP test, Docker build and startup pass |
| Security scan | No secrets; dependency/container findings triaged |
| Rollback rehearsal | Database and application rollback documented and tested |
| Versioning | New version, migration head and build manifest recorded |

---

# Final assessment

The ten-phase roadmap is well conceived and generally sequenced correctly. The implementation contains useful foundations for every phase, especially scan-run tracking, soft blocking, candidate provenance, durable job records, recommendation proposals and approved-fact modelling.

However, the package should be described as a **substantial implementation candidate**, not a completed or release-approved ten-phase build. The most important remaining corrections are:

1. restore a green, trustworthy full regression suite;
2. enforce approval/export eligibility centrally;
3. make evidence and approved facts genuinely immutable;
4. harden worker lease concurrency;
5. complete actual service extraction from routers;
6. add learning-agent permissions and promotion governance;
7. prove migrations and runtime behaviour on PostgreSQL;
8. make the release artifact reproducible and portable.

# Phase 8E Resolver Refactor Notes

## Current Flow Found

- Batch orchestration: `app/routers/batches.py::_process_batch_job`
- Extraction provider work: `app/services/extractor.py::process_pdf_page` and `process_pdf_page_rows`
- Azure DI integration: `app/services/extractor.py::azure_di_available` and `azure_di_extract_invoice`
- OCR.space/local OCR integration: `app/services/ocr.py`, called through `_get_fallback_ocr_text`
- Native PDF text extraction: `app/services/extractor.py::extract_native_pdf_page`
- Saved-region replay: `app/routers/batches.py::_apply_remap_hints`
- Correction rule application: `app/routers/batches.py::_apply_saved_rules`
- Supplier master/history lookup: `_resolve_supplier_identity`, `_apply_account_suggestions`, and `app/services/supplier_history.py`
- Totals reconciliation: `app/services/totals_reconciliation.py::reconcile_invoice_totals`
- BCRS/deposit split: `app/routers/batches.py::_decide_bcrs_split`
- Arbitration/candidate persistence: `app/services/invoice_arbitration.py::arbitrate_invoice_row` and `persist_field_candidates`
- Review evidence/API: `app/routers/review.py` and `app/static/js/review.js`
- Export filter: `app/services/corrected_exporter.py::build_corrected_rows`, which excludes rows with deleted corrections

## Provider Order

Before: native text was auxiliary, OCR ran first, then rule extraction, then Azure DI/OpenAI vision.

After: Azure DI is attempted before OCR when enabled/configured and not circuit-broken; native text is used only when useful; OCR remains the fallback. DI/native/OCR values are persisted as labelled candidates rather than being blindly trusted.

## Resolver Split

Strong resolver fields:
- supplier_name
- supplier_posting_account
- nominal_account_code
- net_amount
- vat_amount
- total_amount
- tax_code

Validation-only fields:
- invoice_number
- invoice_date
- description
- currency

Invoice number/date candidates from supplier history or broad rules are recorded where present but not used for overwrites. Current-document sources such as Azure DI, native text, OCR.space, saved regions, and manual evidence remain eligible subject to validation.

## Saved Regions

Saved regions still crop-read the current invoice, honour governance filters, try primary regions first, and keep page search capped. Replay status is surfaced as diagnostics with checked fields, changed fields, conflicts, and skipped reasons. Saved supplier-name regions can contribute even when the raw supplier is wrong or incomplete.

## Correction Rules

Business/master-data rules remain active for supplier aliases and nominal/account mapping. Dynamic invoice number/date values are no longer replayed through stale remap rules; they must come from current-document reads or exact text-correction evidence.

## Verification

Ran successfully:
- `node --check app/static/js/review.js`
- `node --check app/static/js/rules.js`

Could not run in this environment because no Python interpreter is installed:
- `python -m compileall app`
- requested `pytest` commands

## Hotfix Note

After deployment review, `process_pdf_page` was corrected so `use_azure_di`
is assigned from `azure_di_available()` before any provider branch reads it.
The previous zip could raise `cannot access local variable 'use_azure_di'`
before reaching native text/OCR fallback.

## Hotfix 2 Note

- Removed the duplicate page-render block from `process_pdf_page`; the page is
  rendered once for DI/OpenAI image extraction, with only the existing size
  fallback render retained.
- Added supplier identity audit evidence via `supplier_identity_apply` and
  `supplier_identity_conflict` audit actions.
- Made account suggestions skip later fuzzy supplier canonicalisation when the
  supplier resolver has already marked an ambiguity, unless VAT evidence is
  available.
- Expanded Apply Saved Regions diagnostics to include saved-region audit events,
  region counts, checked fields, changed values, conflicts, and skipped reasons.

## Hotfix 3 Note

- Tightened the deterministic reading layer before arbitration by using a
  header-focused identity view for invoice number, invoice date, and supplier
  candidate collection.
- Added rule-based supplier VAT extraction so the supplier identity resolver can
  make exact VAT matches even when AI extraction is weak or unavailable.
- Added filtered header/totals text views to reduce footer/legal noise in review
  evidence while keeping full `page_text_raw` available for audit and BCRS logic.
- Added a lightweight currency backfill from filtered header/totals views when
  the primary extraction leaves currency blank.

## Hotfix 4 Note

- Reduced false supplier review pressure for tenants without supplier rules by
  allowing a plausible current-document supplier to be trusted as
  `document_header` / `document_header_vat` when the document evidence is strong.
- Tightened invoice-number selection so short header refs and trailing-slash
  tokens are treated as weak candidates, while longer label-supported invoice
  ids are preferred.
- Prevented `merge_ai_fields` from replacing a stronger invoice number with a
  weaker Azure DI/OpenAI value unless the AI value materially scores better.
- Updated arbitration so trusted document-header suppliers are not automatically
  treated as weak values later in the row lifecycle.

## Hotfix 5 Note

- Persisted richer Azure DI candidate evidence per field so Review can show the
  DI value together with VAT/header/totals excerpts that support it.
- Added `di_candidate_summary` to export rows so the workbook Evidence sheet can
  show what Azure DI read for supplier, invoice/date, totals, and currency on
  each row without a schema change.

## Hotfix 6 Note

- Preserved raw structured Azure DI fields before merge in `_di_structured_fields`
  so Review/export can inspect what DI returned independently of later merge logic.
- Added field-level provenance for raw candidates using `_field_sources`, so a
  supplier read from header rules is no longer automatically labelled as DI just
  because the page used Azure DI overall.
- Persisted separate `azure_di_structured` candidates alongside merged row
  values, allowing direct comparison between raw DI structured output and the
  final selected field in Review and export evidence.
- Adjusted Azure DI supplier merge policy to prefer the structured vendor name
  more consistently when its confidence is strong enough.
- Corrected a Hotfix 6 regression in `simple_extract()` where currency
  provenance referenced an undefined local and caused page processing to fail.
- Added extractor build tag logging (`phase8e_hotfix6b`) at page-processing
  start so deployed logs can confirm the live artifact unambiguously.

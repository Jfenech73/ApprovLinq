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


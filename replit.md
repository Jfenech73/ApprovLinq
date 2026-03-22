# ApprovLinq - Invoice Scanner Service

## Overview
A FastAPI-based invoice scanning service that processes PDF invoices, extracts fields using OCR and OpenAI, stores data in PostgreSQL, and exports to Excel.

## Architecture
- **Backend + Frontend**: FastAPI serves both the API and static HTML frontend from the same server
- **Database**: Replit-managed PostgreSQL (connected via DATABASE_URL secret)
- **OCR**: OCR.space cloud API (key set via OCR_SPACE_API_KEY env var)
- **AI**: OpenAI GPT-4.1-mini for field resolution (key in OPENAI_API_KEY secret)

## Project Structure
```
ApprovLinq/
├── app/
│   ├── main.py          # FastAPI app entry point, serves static files
│   ├── config.py        # Pydantic settings
│   ├── db/
│   │   ├── models.py    # SQLAlchemy models
│   │   └── session.py   # DB engine setup (auto-converts URL format)
│   ├── routers/         # API route handlers (auth, admin, batches, tenant, health)
│   ├── services/        # OCR, extraction, export logic
│   ├── utils/           # Security helpers
│   └── static/          # HTML/CSS/JS frontend files
├── requirements.txt
└── seed_phase3_admin.py  # Admin seeding script
```

## Running the App
- **Workflow**: `cd ApprovLinq && uvicorn app.main:app --host 0.0.0.0 --port 5000`
- **Port**: 5000

## Environment Variables
Configured as Replit env vars (shared):
- APP_NAME, APP_ENV, OPENAI_MODEL, UPLOAD_DIR, EXPORT_DIR, USE_OPENAI
- OCR_PROVIDER (ocr_space), OCR_SPACE_API_KEY, OCR_SPACE_* settings
- ENABLE_PADDLE_OCR=false (PaddleOCR disabled)

Secrets:
- DATABASE_URL (Replit-managed PostgreSQL)
- OPENAI_API_KEY

## Key API Routes
- `GET /` - Login page
- `GET /health` - Health check
- `POST /batches` - Create invoice batch
- `POST /batches/{id}/upload` - Upload invoice PDF
- `POST /batches/{id}/process` - Process invoices
- `GET /batches/{id}/export.xlsx` - Export to Excel

## Service Modules (app/services/)
| Module | Purpose |
|---|---|
| `extractor.py` | 4-stage extraction pipeline — primary extraction + all hooks |
| `parse_dates.py` | Strict day-first date parsing with ambiguity detection |
| `normalize_suppliers.py` | Supplier name matching against suppliers.yaml (VAT/alias/fuzzy) |
| `validate_invoice.py` | Arithmetic reconciliation — deposit/discount/mismatch detection |
| `classify_lines.py` | Hybrid nominal account classification (supplier_rule→taxonomy→historical→keyword→llm→default) |
| `review_engine.py` | Weighted triage → priority bands (high/medium/low) + auto-approval |
| `preprocess.py` | Page image quality scoring (PIL contrast/sharpness) |
| `exporter.py` | Excel export with all new columns |

## Config Files
- `config/suppliers.yaml` — supplier master list with canonical names, VAT, aliases, nominal hints
- `config/product_taxonomy.yaml` — brand/product YAML with nominal account hints and OCR aliases

## Extraction Pipeline (4-Stage)
`process_pdf_page` in `app/services/extractor.py`:
1. **Stage 1** — Acquire + preprocess + quality score (PIL contrast/sharpness/bleed-suppression, quality 0–1)
2. **Stage 2** — Field extraction: rule-based → Azure DI (primary) → OpenAI vision → OpenAI text → validation pass
3. **Stage 3** — `validate_invoice` for arithmetic reconciliation; `normalize_supplier` for canonical name + match_method; `parse_invoice_date` for ambiguity check
4. **Stage 4** — `compute_review_decision` for consolidated priority/reasons/fields/auto_approved; confidence scoring with quality penalty

## Nominal Account Classification (Hybrid Order in batches.py)
`_apply_account_suggestions`: A. supplier DB default_nominal (explicit) → `classify_line` pipeline (supplier_rule from config → taxonomy_match → historical_similarity → keyword_match → llm_fallback → default)

## Key DB Columns (v4+)
- `tenant_suppliers.vat_number` — supplier VAT for authoritative matching
- `invoice_rows.review_priority` — high/medium/low triage band
- `invoice_rows.review_reasons` — pipe-separated reason codes
- `invoice_rows.review_fields` — pipe-separated field names that need review
- `invoice_rows.auto_approved` — TRUE when all reasons are low-severity
- `invoice_rows.page_quality_score` — scan quality 0.0–1.0
- `invoice_rows.classification_method` — how the nominal was determined
- `invoice_rows.supplier_match_method` — vat_match/alias_match/fuzzy_match/unmatched
- `invoice_rows.totals_reconciliation_status` — ok/ok_with_deposit/totals_mismatch/vat_anomaly/etc.

## Tests (77 cases)
`ApprovLinq/tests/` — pytest suite covering all pipeline modules:
- `test_parse_dates.py`, `test_normalize_suppliers.py`, `test_validate_invoice.py`, `test_classify_lines.py`, `test_review_engine.py`

## Deployment
Configured for autoscale deployment on Replit.

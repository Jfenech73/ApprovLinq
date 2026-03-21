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

## Extraction Pipeline (4-Stage)
`process_pdf_page` in `app/services/extractor.py`:
1. **Stage 1** — Acquire + preprocess + quality score (PIL contrast/sharpness/bleed-suppression, quality 0–1)
2. **Stage 2** — Field extraction: rule-based → Azure DI (primary) → OpenAI vision → OpenAI text → validation pass
3. **Stage 3** — Line normalization: Net→Total fallback, deposit/BCRS detection, supplier name normalisation
4. **Stage 4** — Accounting prep: confidence scoring (with quality penalty), review reason codes, validation_status, evidence strings

## Nominal Account Classification (Hybrid Order in batches.py)
`_apply_account_suggestions`: VAT match → fuzzy name → A. supplier default_nominal → B. historical nominal → C. keyword → D. brand taxonomy → E. is_default fallback

## Key DB Columns (v3.80+)
- `tenant_suppliers.vat_number` — supplier VAT for authoritative matching
- `invoice_rows.review_reasons` — pipe-separated reason codes (no_supplier, invoice_number_missing, vat_anomaly, totals_mismatch, deposit_component_detected, etc.)
- `invoice_rows.page_quality_score` — scan quality 0.0–1.0

## Deployment
Configured for autoscale deployment on Replit.

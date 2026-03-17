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

## Deployment
Configured for autoscale deployment on Replit.

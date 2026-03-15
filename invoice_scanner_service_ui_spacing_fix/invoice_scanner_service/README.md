# Invoice Scanner Service

A production-oriented FastAPI service for scanning invoice PDFs page by page, extracting fields, storing rows in Neon/Postgres, and exporting the batch to Excel.

## What changed
- Uses **PyMuPDF** block-based text extraction first
- Uses **OCR.space** as the default OCR fallback for hard/scanned pages
- Supports optional **PaddleOCR** if you prefer a local OCR engine later
- Stores invoice rows in a stable Neon/Postgres schema
- Adds richer Excel export with `Invoices`, `Needs Review`, and `Summary`
- Keeps a safe optional admin endpoint for creating a custom physical table by name

## Recommended extraction flow
1. Native PDF text extraction (fastest and cheapest)
2. OCR fallback only when the page text is too thin or broken
3. Optional OpenAI-assisted field resolution
4. Validation + review flags
5. Export to Excel on demand

## Main API routes
- `GET /health`
- `POST /batches`
- `GET /batches`
- `POST /batches/{batch_id}/upload`
- `POST /batches/{batch_id}/process`
- `GET /batches/{batch_id}/rows`
- `GET /batches/{batch_id}/export.xlsx`
- `POST /admin/create-custom-table/{table_name}`

## OCR default
This build is set up to avoid local Tesseract by default.

Set:
- `OCR_PROVIDER=ocr_space`
- `OCR_SPACE_API_KEY=your_key`

If you want no OCR fallback at all, set:
- `OCR_PROVIDER=none`

If you later want local OCR instead of cloud OCR, set:
- `OCR_PROVIDER=paddleocr`
- install PaddleOCR in the image

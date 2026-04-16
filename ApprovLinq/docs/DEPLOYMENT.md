# Deploying the Invoice Scanner Service to Neon + Koyeb

## 1. What this build uses
- **Neon** for Postgres storage
- **Koyeb** for running the FastAPI service
- **OCR.space** as the default OCR fallback
- **OpenAI** optionally for invoice field resolution

This build avoids Tesseract by default.

---

## 2. Create your Neon database
1. Log in to Neon.
2. Create a new project.
3. Copy the **direct connection string** for schema setup.
4. Open the Neon SQL editor.
5. Run the SQL from `sql/schema.sql`.
6. Copy the **pooled connection string** for the running app.

Use the direct connection for migrations/schema setup and the pooled connection for the live app.

---

## 3. Prepare the project locally
1. Extract the ZIP.
2. Open the folder in VS Code.
3. Copy `.env.example` to `.env`.
4. Fill in at least:

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
USE_OPENAI=true
OPENAI_API_KEY=your_openai_key
OCR_PROVIDER=ocr_space
OCR_SPACE_API_KEY=your_ocr_space_key
```

If you do not want LLM support yet:

```env
USE_OPENAI=false
```

If you want to test only native PDF extraction and no OCR fallback:

```env
OCR_PROVIDER=none
```

---

## 4. Run locally
Create a virtual environment and install dependencies.

### Windows PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Mac/Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open:
- `http://127.0.0.1:8000/docs`

Test in this order:
1. `POST /batches`
2. `POST /batches/{id}/upload`
3. `POST /batches/{id}/process`
4. `GET /batches/{id}/rows`
5. `GET /batches/{id}/export.xlsx`

---

## 5. Push to GitHub
1. Create a new GitHub repository.
2. Upload the project files.
3. Commit and push.

---

## 6. Deploy on Koyeb
1. Log in to Koyeb.
2. Create a new Web Service.
3. Choose GitHub as the source.
4. Select your repository.
5. Koyeb should detect the `Dockerfile`.
6. Expose port `8000`.
7. Add these environment variables:

```env
APP_ENV=production
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
USE_OPENAI=true
OPENAI_API_KEY=your_openai_key
OCR_PROVIDER=ocr_space
OCR_SPACE_API_KEY=your_ocr_space_key
OCR_SPACE_ENDPOINT=https://api.ocr.space/parse/image
OCR_SPACE_LANGUAGE=eng
OCR_SPACE_OVERLAY_REQUIRED=false
OCR_SPACE_SCALE=true
OCR_SPACE_OCR_ENGINE=2
OCR_SPACE_TIMEOUT_SECONDS=90
```

8. Deploy the service.

---

## 7. Test in production
After Koyeb deploys, open:
- `https://your-service.koyeb.app/docs`

Then run the same sequence:
1. create batch
2. upload PDF
3. process
4. review rows
5. export Excel

---

## 8. Notes on OCR modes
### OCR.space default
This is the easiest production mode because it avoids local OCR binaries.

### No OCR fallback
If most of your PDFs already contain native text, set:
```env
OCR_PROVIDER=none
```

### PaddleOCR later
If you later want self-hosted OCR, use:
```env
OCR_PROVIDER=paddleocr
```
Then add PaddleOCR to `requirements.txt` and rebuild the image.

---

## 9. Common issues
### Processing works for some PDFs but not image-only scans
- Make sure `OCR_PROVIDER=ocr_space`
- Make sure `OCR_SPACE_API_KEY` is correct
- Check whether the free OCR.space limits are being hit

### OCR.space fails on a page
- Retry the same batch later
- Check page image quality
- Try `OCR_SPACE_OCR_ENGINE=1` if engine 2 is unstable for a specific supplier

### Database errors on Koyeb
- Verify the pooled Neon connection string
- Confirm `sslmode=require`
- Test the schema in Neon SQL editor first

### OpenAI extraction fails
- Set `USE_OPENAI=false` to test pure native/OCR extraction first
- Verify the API key and model name

---

## 10. Recommended production path
Start with:
- native PDF extraction first
- OCR.space fallback
- OpenAI enabled only if needed for messy invoices

Once the workflow is stable, you can decide whether to stay with OCR.space or move to a stronger invoice-focused OCR provider.

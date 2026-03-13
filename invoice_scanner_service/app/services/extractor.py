from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.services.ocr import OCRSpaceBackend, PaddleOCRBackend
from app.config import settings


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_meaningful_chars(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]", text or ""))


def parse_amount(value: str | None) -> float | None:
    if not value:
        return None
    raw = str(value).strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if re.match(r"^\d{1,3}(\.\d{3})+,\d{2}$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.match(r"^\d+,\d{2}$", raw):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def parse_date(value: str | None):
    if not value:
        return None
    patterns = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def first_match(patterns: list[str], text: str, group: int = 1) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(group).strip()
    return None


def extract_native_pdf_pages(pdf_path: str | Path) -> list[str]:
    doc = fitz.open(str(pdf_path))
    try:
        pages: list[str] = []
        for page in doc:
            blocks = page.get_text("blocks")
            blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
            text = "\n".join(b[4].strip() for b in blocks if len(b) > 4 and b[4].strip())
            pages.append(clean_text(text))
        return pages
    finally:
        doc.close()


def get_ocr_backend():
    provider = (settings.ocr_provider or "none").strip().lower()
    if provider == "ocr_space":
        return OCRSpaceBackend()
    if provider == "paddleocr":
        return PaddleOCRBackend()
    return None


def simple_extract(text: str) -> dict[str, Any]:
    invoice_number = first_match([
        r"invoice\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
        r"\binv(?:oice)?\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
    ], text)

    invoice_date_raw = first_match([
        r"invoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\bdate\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
    ], text)
    invoice_date = parse_date(invoice_date_raw)

    total_raw = first_match([
        r"(?:amount due|balance due|grand total|total due|total)\s*[:\-]?\s*(?:EUR|€)?\s*([0-9.,]+)"
    ], text)
    total_amount = parse_amount(total_raw)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    supplier_name = lines[0][:200] if lines else None

    return {
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "description": "Diagnostic extraction",
        "line_items_raw": None,
        "net_amount": None,
        "vat_amount": None,
        "total_amount": total_amount,
        "currency": "EUR" if ("€" in text or "eur" in text.lower()) else None,
        "tax_code": None,
    }


def process_pdf(pdf_path: str | Path, openai_api_key: str | None = None) -> list[dict[str, Any]]:
    pdf_path = Path(pdf_path)
    native_pages = extract_native_pdf_pages(pdf_path)
    ocr_backend = get_ocr_backend()

    results: list[dict[str, Any]] = []

    for idx, native_text in enumerate(native_pages):
        native_text = clean_text(native_text)
        final_text = native_text
        method = "native_text"
        ocr_error = None

        # Simple rule: if native text is weak, try OCR
        if count_meaningful_chars(native_text) < 80 and ocr_backend is not None:
            try:
                ocr_text = clean_text(
                    ocr_backend.extract_text_from_pdf_page(pdf_path, idx, scale=2.5)
                )
                if count_meaningful_chars(ocr_text) > count_meaningful_chars(native_text):
                    final_text = ocr_text
                    method = f"ocr_{ocr_backend.name}"
            except Exception as e:
                ocr_error = str(e)

        # Always return one row per page
        if count_meaningful_chars(final_text) == 0:
            final_text = f"OCR/NATIVE TEXT EMPTY. OCR_ERROR={ocr_error or 'none'}"
            method = f"{method}_empty"

        extracted = simple_extract(final_text)
        extracted.update({
            "page_no": idx + 1,
            "method_used": method,
            "confidence_score": 0.10 if "EMPTY" in final_text else 0.50,
            "validation_status": "review",
            "review_required": True,
            "header_raw": "\n".join(final_text.splitlines()[:12]),
            "totals_raw": None,
            "page_text_raw": final_text[:20000],
        })
        results.append(extracted)

    return results

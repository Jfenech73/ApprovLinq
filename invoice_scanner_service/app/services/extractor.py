from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

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


def suspicious_invoice_number(value: str | None) -> bool:
    if not value:
        return True
    v = str(value).strip().lower()
    bad = {"to", "from", "date", "invoice", "invoice no", "invoice number", "page"}
    if v in bad:
        return True
    if len(v) < 3:
        return True
    return False


def find_supplier_name(text: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    skip = r"invoice|tax|vat|date|page|customer|bill to|ship to|total|amount due|balance due"

    for line in lines[:12]:
        if len(line) < 3:
            continue
        if re.search(skip, line, re.I):
            continue
        if len(line) > 100:
            continue
        return line[:200]

    return None


def simple_extract(text: str) -> dict[str, Any]:
    invoice_number = first_match([
        r"invoice\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
        r"\binv(?:oice)?\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
        r"\bdocument\s*(?:no|number)\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
    ], text)

    invoice_date_raw = first_match([
        r"invoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\bdate\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
    ], text)
    invoice_date = parse_date(invoice_date_raw)

    net_raw = first_match([
        r"(?:subtotal|sub total|net amount|amount excl(?:uding)? vat|taxable amount)\s*[:\-]?\s*(?:EUR|€)?\s*([0-9.,]+)"
    ], text)
    vat_raw = first_match([
        r"(?:vat|tax|iva)\s*[:\-]?\s*(?:EUR|€)?\s*([0-9.,]+)"
    ], text)
    total_raw = first_match([
        r"(?:amount due|balance due|grand total|total due|total amount|total)\s*[:\-]?\s*(?:EUR|€)?\s*([0-9.,]+)"
    ], text)

    net_amount = parse_amount(net_raw)
    vat_amount = parse_amount(vat_raw)
    total_amount = parse_amount(total_raw)

    supplier_name = find_supplier_name(text)

    return {
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "description": "Invoice extraction",
        "line_items_raw": None,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": "EUR" if ("€" in text or "eur" in text.lower()) else None,
        "tax_code": None,
    }


def openai_extract_invoice_fields(
    page_text: str,
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> dict[str, Any] | None:
    if not api_key or not page_text.strip():
        return None

    prompt = (
        "Extract invoice fields from this ONE invoice page.\n"
        "Return strict JSON only with keys:\n"
        "supplier_name, invoice_number, invoice_date, description, "
        "net_amount, vat_amount, total_amount, currency, tax_code.\n"
        "Rules:\n"
        "- Use null when unknown.\n"
        "- Do not guess.\n"
        "- invoice_date should be DD/MM/YYYY when present.\n"
        "- amounts should be plain numbers.\n"
        "- description max 20 words.\n\n"
        f"PAGE TEXT:\n{page_text[:12000]}"
    )

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": prompt,
                "max_output_tokens": 300,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        text_parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    txt = content.get("text", "")
                    if txt:
                        text_parts.append(txt)

        raw = " ".join(text_parts).strip()
        if not raw:
            return None

        m = re.search(r"\{.*\}", raw, re.S)
        payload = json.loads(m.group(0) if m else raw)

        return {
            "supplier_name": payload.get("supplier_name"),
            "invoice_number": payload.get("invoice_number"),
            "invoice_date": parse_date(payload.get("invoice_date")) if payload.get("invoice_date") else None,
            "description": payload.get("description"),
            "net_amount": parse_amount(payload.get("net_amount")) if payload.get("net_amount") is not None else None,
            "vat_amount": parse_amount(payload.get("vat_amount")) if payload.get("vat_amount") is not None else None,
            "total_amount": parse_amount(payload.get("total_amount")) if payload.get("total_amount") is not None else None,
            "currency": payload.get("currency"),
            "tax_code": payload.get("tax_code"),
        }
    except Exception:
        return None


def needs_ai_fallback(extracted: dict[str, Any], text: str) -> bool:
    if count_meaningful_chars(text) < 20:
        return False

    if suspicious_invoice_number(extracted.get("invoice_number")):
        return True
    if extracted.get("invoice_date") is None:
        return True
    if extracted.get("total_amount") is None:
        return True

    return False


def merge_ai_fields(base: dict[str, Any], ai: dict[str, Any] | None) -> dict[str, Any]:
    if not ai:
        return base

    merged = dict(base)

    if not merged.get("supplier_name") and ai.get("supplier_name"):
        merged["supplier_name"] = ai.get("supplier_name")

    if suspicious_invoice_number(merged.get("invoice_number")) and ai.get("invoice_number"):
        merged["invoice_number"] = ai.get("invoice_number")

    if merged.get("invoice_date") is None and ai.get("invoice_date") is not None:
        merged["invoice_date"] = ai.get("invoice_date")

    if merged.get("net_amount") is None and ai.get("net_amount") is not None:
        merged["net_amount"] = ai.get("net_amount")

    if merged.get("vat_amount") is None and ai.get("vat_amount") is not None:
        merged["vat_amount"] = ai.get("vat_amount")

    if merged.get("total_amount") is None and ai.get("total_amount") is not None:
        merged["total_amount"] = ai.get("total_amount")

    if not merged.get("currency") and ai.get("currency"):
        merged["currency"] = ai.get("currency")

    if not merged.get("tax_code") and ai.get("tax_code"):
        merged["tax_code"] = ai.get("tax_code")

    if merged.get("description") in (None, "", "Invoice extraction") and ai.get("description"):
        merged["description"] = ai.get("description")

    return merged


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

        # Reintroduce AI only as fallback
        if (
            settings.use_openai
            and openai_api_key
            and needs_ai_fallback(extracted, final_text)
        ):
            ai_fields = openai_extract_invoice_fields(
                final_text,
                openai_api_key,
                model=settings.openai_model,
            )
            extracted = merge_ai_fields(extracted, ai_fields)
            method = f"{method}+openai_fallback"

        # Better confidence scoring
        confidence = 0.0
        if extracted.get("supplier_name"):
            confidence += 0.15
        if not suspicious_invoice_number(extracted.get("invoice_number")):
            confidence += 0.20
        if extracted.get("invoice_date"):
            confidence += 0.20
        if extracted.get("total_amount") is not None:
            confidence += 0.20
        if extracted.get("net_amount") is not None:
            confidence += 0.10
        if extracted.get("vat_amount") is not None:
            confidence += 0.10
        if (
            extracted.get("net_amount") is not None
            and extracted.get("vat_amount") is not None
            and extracted.get("total_amount") is not None
        ):
            if round((extracted["net_amount"] + extracted["vat_amount"]) - extracted["total_amount"], 2) == 0:
                confidence += 0.05

        confidence = round(min(confidence, 0.99), 2)

        extracted.update({
            "page_no": idx + 1,
            "method_used": method,
            "confidence_score": confidence,
            "validation_status": "ok" if confidence >= 0.70 else "review",
            "review_required": confidence < 0.70,
            "header_raw": "\n".join(final_text.splitlines()[:12]),
            "totals_raw": "\n".join(final_text.splitlines()[-10:]) if final_text else None,
            "page_text_raw": final_text[:20000],
        })
        results.append(extracted)

    return results
from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Optional

import fitz
import pypdfium2 as pdfium
from dateutil import parser as dateparser

from app.config import settings
from app.services.ocr import OCRSpaceBackend, PaddleOCRBackend

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

@dataclass
class ExtractionResult:
    page_no: int
    supplier_name: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[date]
    description: Optional[str]
    net_amount: Optional[float]
    vat_amount: Optional[float]
    total_amount: Optional[float]
    currency: Optional[str]
    tax_code: Optional[str]
    method_used: str
    confidence_score: float
    validation_status: str
    review_required: bool
    header_raw: Optional[str]
    totals_raw: Optional[str]
    page_text_raw: Optional[str]

def clean_text(s: str, max_chars: int = 15000) -> str:
    s = (s or "").replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > max_chars:
        head = s[: max_chars // 2]
        tail = s[-max_chars // 2 :]
        s = head + "\n\n...[TRUNCATED]...\n\n" + tail
    return s

def parse_amount_to_float(amount_str: Optional[str]) -> Optional[float]:
    if not amount_str:
        return None
    s = re.sub(r"[^\d,\.\-]", "", amount_str.strip())
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def normalize_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    try:
        return dateparser.parse(date_str, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None

def amounts_validate(net: Optional[float], vat: Optional[float], total: Optional[float], tol: float = 0.03) -> bool:
    if net is None or vat is None or total is None:
        return False
    return abs((net + vat) - total) <= tol

def extract_text_blocks(pdf_path: Path) -> list[str]:
    doc = fitz.open(pdf_path)
    pages: list[str] = []
    for page in doc:
        blocks = page.get_text("blocks")
        blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
        text = "\n".join(b[4].strip() for b in blocks if len(b) > 4 and b[4].strip())
        pages.append(clean_text(text))
    doc.close()
    return pages

def render_page_to_jpeg_b64(pdf_path: Path, page_index: int, scale: float = 2.7) -> str:
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf.get_page(page_index)
    image = page.render(scale=scale).to_pil()
    page.close()
    pdf.close()
    buf = BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def regex_extract(page_text: str) -> dict:
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    page_text_lower = page_text.lower()

    date_match = re.search(r"(?:invoice\s*date|date|issued)[:\s]*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})", page_text, re.I)
    inv_match = re.search(r"(?:invoice\s*(?:no|number|#)|document\s*no)[:\s]*([A-Z0-9\-\/]+)", page_text, re.I)

    amount_patterns = {
        "net": r"(?:subtotal|net\s*amount|amount\s*excl(?:uding)?\s*vat|taxable\s*amount)[:\s€]*([\d.,]+)",
        "vat": r"(?:vat|tax|iva)[:\s€]*([\d.,]+)",
        "total": r"(?:grand\s*total|total\s*amount|amount\s*due|total)[:\s€]*([\d.,]+)"
    }
    out = {
        "invoice_date": date_match.group(1) if date_match else None,
        "invoice_number": inv_match.group(1) if inv_match else None,
        "supplier_name": lines[0][:200] if lines else None,
        "currency": "EUR" if ("€" in page_text or "eur" in page_text_lower) else None,
        "description": None,
    }
    for key, pat in amount_patterns.items():
        m = re.search(pat, page_text, re.I)
        out[key] = parse_amount_to_float(m.group(1)) if m else None

    # header/totals evidence
    out["header_raw"] = "\n".join(lines[:8]) if lines else None
    out["totals_raw"] = "\n".join(lines[-10:]) if lines else None

    if any(word in page_text_lower for word in ["fuel", "diesel", "petrol"]):
        out["description"] = "Fuel purchase"
    elif any(word in page_text_lower for word in ["food", "brisket", "meat", "catering"]):
        out["description"] = "Food or catering purchase"
    elif any(word in page_text_lower for word in ["office", "stationery", "paper"]):
        out["description"] = "Office supplies"
    return out

def openai_extract(page_text: str, image_b64: Optional[str] = None) -> dict:
    if not settings.use_openai or not settings.openai_api_key or OpenAI is None:
        return {}
    client = OpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Extract invoice fields from ONE invoice page. "
        "Return strict JSON only with keys: supplier_name, invoice_number, invoice_date, description, "
        "net_amount, vat_amount, total_amount, currency, tax_code. "
        "Use null when absent. Do not guess."
    )
    content = [{"type": "input_text", "text": prompt + "\n\nPAGE TEXT:\n" + page_text[:12000]}]
    if image_b64:
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}"})
    try:
        resp = client.responses.create(
            model=settings.openai_model,
            input=[{"role": "user", "content": content}],
        )
        text = getattr(resp, "output_text", "") or ""
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else json.loads(text)
        for k in ("net_amount", "vat_amount", "total_amount"):
            data[k] = parse_amount_to_float(str(data[k])) if data.get(k) is not None else None
        if data.get("invoice_date"):
            dt = normalize_date(data["invoice_date"])
            data["invoice_date"] = dt.isoformat() if dt else None
        return data
    except Exception:
        return {}

def combine_candidates(regex_data: dict, ai_data: dict) -> dict:
    final = {}
    keys = ["supplier_name","invoice_number","invoice_date","description","net_amount","vat_amount","total_amount","currency","tax_code"]
    for k in keys:
        final[k] = ai_data.get(k) if ai_data.get(k) not in (None, "", "null") else regex_data.get(k)
    final["header_raw"] = regex_data.get("header_raw")
    final["totals_raw"] = regex_data.get("totals_raw")
    return final

def score_result(data: dict) -> tuple[float, str, bool]:
    score = 0.0
    if data.get("invoice_number"): score += 0.20
    if data.get("invoice_date"): score += 0.20
    if data.get("supplier_name"): score += 0.10
    if data.get("total_amount") is not None: score += 0.20
    if data.get("net_amount") is not None: score += 0.15
    if data.get("vat_amount") is not None: score += 0.15
    valid = amounts_validate(data.get("net_amount"), data.get("vat_amount"), data.get("total_amount"))
    if valid: score += 0.10
    score = min(score, 1.00)
    if valid:
        return score, "passed", score < 0.70
    return score, "totals_mismatch_or_missing", score < 0.80

def choose_ocr_backend():
    provider = (settings.ocr_provider or "").strip().lower()
    if not provider and settings.enable_paddle_ocr:
        provider = "paddleocr"
    if provider in ("none", "off", "disabled"):
        return None
    if provider == "paddleocr":
        return PaddleOCRBackend()
    if provider == "ocr_space":
        return OCRSpaceBackend()
    raise RuntimeError(f"Unsupported OCR provider: {provider}")

def process_pdf(pdf_path: Path) -> list[ExtractionResult]:
    native_pages = extract_text_blocks(pdf_path)
    ocr = choose_ocr_backend()
    results: list[ExtractionResult] = []

    for idx, native_text in enumerate(native_pages):
        page_text = native_text
        method = "native_text"
        image_b64 = None

        if len(native_text.strip()) < 80 and ocr is not None:
            try:
                ocr_text = clean_text(ocr.extract_text_from_pdf_page(pdf_path, idx))
            except Exception:
                ocr_text = ""
            if len(ocr_text) > len(native_text):
                page_text = ocr_text
                method = f"ocr:{ocr.name}"

        regex_data = regex_extract(page_text)

        if settings.use_openai and settings.openai_api_key:
            image_b64 = render_page_to_jpeg_b64(pdf_path, idx)
            ai_data = openai_extract(page_text, image_b64=image_b64)
            merged = combine_candidates(regex_data, ai_data)
            method = method + "+openai"
        else:
            merged = combine_candidates(regex_data, {})

        inv_date = normalize_date(merged.get("invoice_date")) if isinstance(merged.get("invoice_date"), str) else merged.get("invoice_date")
        merged["invoice_date"] = inv_date
        score, validation_status, review_required = score_result(merged)

        results.append(
            ExtractionResult(
                page_no=idx + 1,
                supplier_name=merged.get("supplier_name"),
                invoice_number=merged.get("invoice_number"),
                invoice_date=merged.get("invoice_date"),
                description=merged.get("description"),
                net_amount=merged.get("net_amount"),
                vat_amount=merged.get("vat_amount"),
                total_amount=merged.get("total_amount"),
                currency=merged.get("currency") or "EUR",
                tax_code=merged.get("tax_code"),
                method_used=method,
                confidence_score=round(score, 2),
                validation_status=validation_status,
                review_required=review_required,
                header_raw=merged.get("header_raw"),
                totals_raw=merged.get("totals_raw"),
                page_text_raw=page_text[:20000],
            )
        )
    return results

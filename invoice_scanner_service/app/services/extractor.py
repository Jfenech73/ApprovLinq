from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

from app.config import settings
from app.services.ocr import OCRSpaceBackend, PaddleOCRBackend


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = text.replace("|", " ")
    text = text.replace("€", " EUR ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_meaningful_chars(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]", text or ""))


def looks_meaningful(text: str, threshold: int = 40) -> bool:
    return count_meaningful_chars(text) >= threshold


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

    if provider == "none":
        return None
    if provider == "ocr_space":
        return OCRSpaceBackend()
    if provider == "paddleocr":
        return PaddleOCRBackend()
    return None


def extract_pdf_pages(pdf_path: str | Path) -> list[dict[str, Any]]:
    pdf_path = Path(pdf_path)
    native_pages = extract_native_pdf_pages(pdf_path)
    ocr_backend = get_ocr_backend()

    output: list[dict[str, Any]] = []

    for idx, native_text in enumerate(native_pages):
        native_text = clean_text(native_text or "")
        final_text = native_text
        method = "native_text"
        ocr_error = None

        # Use OCR aggressively when native text is weak/junky.
        should_try_ocr = count_meaningful_chars(native_text) < 120

        if should_try_ocr and ocr_backend is not None:
            try:
                ocr_text = clean_text(
                    ocr_backend.extract_text_from_pdf_page(pdf_path, idx, scale=3.5)
                )
                # If OCR produced useful text, trust it directly.
                if count_meaningful_chars(ocr_text) > 10:
                    final_text = ocr_text
                    method = f"ocr_{ocr_backend.name}"
            except Exception as e:
                ocr_error = str(e)

        output.append(
            {
                "page_no": idx + 1,
                "text": final_text,
                "text_source": method,
                "ocr_error": ocr_error,
            }
        )

    return output


def parse_amount(value: str | None) -> float | None:
    if value is None:
        return None

    raw = str(value).strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if not raw:
        return None

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


def extract_totals_region(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    markers = (
        "subtotal",
        "sub total",
        "vat",
        "tax",
        "total",
        "gross",
        "net amount",
        "amount due",
        "balance due",
        "grand total",
        "total due",
    )

    start = int(len(lines) * 0.5)
    candidates = lines[start:] + lines[:start]

    selected = [ln for ln in candidates if any(m in ln.lower() for m in markers)]
    return "\n".join(selected[:20]).strip()


def extract_candidate_line_items(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept: list[str] = []

    skip_patterns = [
        r"invoice\s*(no|number)",
        r"\bdate\b",
        r"\bvat\b",
        r"\btax\b",
        r"\btotal\b",
        r"\bsubtotal\b",
        r"\bamount due\b",
        r"\bbalance due\b",
        r"\biban\b",
        r"\bbic\b",
        r"\bpage\b",
        r"\bcustomer\b",
        r"\bsupplier\b",
        r"\baddress\b",
        r"\bemail\b",
        r"\bphone\b",
    ]

    for line in lines:
        lower = line.lower()

        if len(line) < 6:
            continue
        if any(re.search(p, lower, re.I) for p in skip_patterns):
            continue
        if re.fullmatch(r"[\d\W]+", line):
            continue

        money_like = len(re.findall(r"\d+[.,]\d{2}", line))
        words_like = len(re.findall(r"[A-Za-z]{3,}", line))

        if words_like >= 2 and (money_like >= 1 or len(line) > 20):
            kept.append(line)

    kept = list(dict.fromkeys(kept))
    return "\n".join(kept[:25]).strip()


def limit_to_20_words(text: str) -> str:
    words = re.findall(r"\S+", (text or "").strip())
    return " ".join(words[:20]).strip()


def summarise_line_items_rule_based(line_items_text: str) -> str:
    text = line_items_text.lower()

    keyword_groups = [
        ("fuel and related vehicle consumables", ["fuel", "diesel", "petrol", "unleaded", "lubricant"]),
        ("office supplies and stationery", ["paper", "stationery", "toner", "ink", "folder", "pen", "notebook"]),
        ("cleaning supplies and hygiene products", ["detergent", "cleaner", "soap", "bleach", "sanitiser", "tissue"]),
        ("food and catering supplies", ["food", "catering", "beverage", "drink", "snack", "bread", "meat"]),
        ("vehicle parts and maintenance items", ["filter", "brake", "tyre", "battery", "engine", "service kit"]),
        ("electrical supplies and components", ["cable", "socket", "switch", "lamp", "electrical", "fuse"]),
        ("building materials and hardware items", ["cement", "paint", "screw", "bolt", "hardware", "tool"]),
        ("printing and marketing materials", ["print", "printing", "flyer", "poster", "banner", "brochure"]),
        ("software, subscriptions, or digital services", ["subscription", "software", "license", "hosting", "domain"]),
        ("professional or business services", ["service", "consulting", "labour", "maintenance", "support"]),
    ]

    for label, words in keyword_groups:
        if any(w in text for w in words):
            return limit_to_20_words(label)

    lines = [ln.strip() for ln in line_items_text.splitlines() if ln.strip()]
    if lines:
        joined = " ".join(lines[:2])
        joined = re.sub(r"\s+", " ", joined).strip()
        return limit_to_20_words(joined)

    return "Invoice goods or services"


def summarise_line_items_with_openai(
    line_items_text: str,
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> str | None:
    if not api_key or not line_items_text.strip():
        return None

    prompt = (
        "You are extracting an accounting-friendly invoice description.\n"
        "Based only on the invoice item lines provided, return one short description.\n"
        "Rules:\n"
        "- Maximum 20 words\n"
        "- Plain business English\n"
        "- No supplier names\n"
        "- No invoice numbers\n"
        "- No amounts\n"
        "- Summarise the goods or services purchased\n"
        "- Return only the description text\n\n"
        f"Invoice item lines:\n{line_items_text}"
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
                "max_output_tokens": 60,
            },
            timeout=45,
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

        result = " ".join(text_parts).strip()
        if result:
            return limit_to_20_words(result)
    except Exception:
        return None

    return None


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
            "description": limit_to_20_words(payload.get("description") or "") if payload.get("description") else None,
            "net_amount": parse_amount(payload.get("net_amount")) if payload.get("net_amount") is not None else None,
            "vat_amount": parse_amount(payload.get("vat_amount")) if payload.get("vat_amount") is not None else None,
            "total_amount": parse_amount(payload.get("total_amount")) if payload.get("total_amount") is not None else None,
            "currency": payload.get("currency"),
            "tax_code": payload.get("tax_code"),
        }
    except Exception:
        return None


def regex_extract(text: str, openai_api_key: str | None = None) -> dict[str, Any]:
    invoice_number = first_match(
        [
            r"invoice\s*(?:no|number|#)\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
            r"\binv(?:oice)?\s*[:\-]?\s*([A-Z0-9\/\-_]+)",
        ],
        text,
    )

    invoice_date_raw = first_match(
        [
            r"invoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
            r"\bdate\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        ],
        text,
    )
    invoice_date = parse_date(invoice_date_raw)

    totals_region = extract_totals_region(text)

    net_raw = first_match(
        [r"(?:subtotal|sub total|net amount|net)\s*[:\-]?\s*(?:EUR)?\s*([0-9.,]+)"],
        totals_region or text,
    )
    vat_raw = first_match(
        [r"(?:vat|tax)\s*[:\-]?\s*(?:EUR)?\s*([0-9.,]+)"],
        totals_region or text,
    )
    total_raw = first_match(
        [r"(?:amount due|balance due|grand total|total due|total)\s*[:\-]?\s*(?:EUR)?\s*([0-9.,]+)"],
        totals_region or text,
    )

    net_amount = parse_amount(net_raw)
    vat_amount = parse_amount(vat_raw)
    total_amount = parse_amount(total_raw)

    supplier_name = find_supplier_name(text)
    line_items_raw = extract_candidate_line_items(text)

    description = None
    if line_items_raw:
        description = summarise_line_items_rule_based(line_items_raw)
        ai_desc = summarise_line_items_with_openai(line_items_raw, openai_api_key) if openai_api_key else None
        if ai_desc:
            description = ai_desc

    # Reintroduce AI field extraction from the original version,
    # but only when regex is weak.
    weak_regex = not any([
        supplier_name,
        invoice_number,
        invoice_date,
        net_amount is not None,
        vat_amount is not None,
        total_amount is not None,
    ])

    if weak_regex and openai_api_key and count_meaningful_chars(text) > 20:
        ai_fields = openai_extract_invoice_fields(text, openai_api_key, model=settings.openai_model)
        if ai_fields:
            supplier_name = supplier_name or ai_fields.get("supplier_name")
            invoice_number = invoice_number or ai_fields.get("invoice_number")
            invoice_date = invoice_date or ai_fields.get("invoice_date")
            description = description or ai_fields.get("description")
            net_amount = net_amount if net_amount is not None else ai_fields.get("net_amount")
            vat_amount = vat_amount if vat_amount is not None else ai_fields.get("vat_amount")
            total_amount = total_amount if total_amount is not None else ai_fields.get("total_amount")

    if not description:
        description = "Invoice goods or services"

    confidence = 0.0
    if supplier_name:
        confidence += 0.15
    if invoice_number:
        confidence += 0.20
    if invoice_date:
        confidence += 0.20
    if total_amount is not None:
        confidence += 0.20
    if line_items_raw:
        confidence += 0.10
    if net_amount is not None and vat_amount is not None and total_amount is not None:
        if round((net_amount + vat_amount) - total_amount, 2) == 0:
            confidence += 0.15

    confidence = round(min(confidence, 0.99), 2)
    review_required = confidence < 0.70
    validation_status = "review" if review_required else "ok"

    return {
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "description": description,
        "line_items_raw": line_items_raw,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": "EUR",
        "tax_code": None,
        "confidence_score": confidence,
        "validation_status": validation_status,
        "review_required": review_required,
        "header_raw": "\n".join(text.splitlines()[:12]),
        "totals_raw": totals_region,
        "page_text_raw": text,
    }


def make_fallback_row(text: str, openai_api_key: str | None = None) -> dict[str, Any]:
    text = clean_text(text)
    supplier_name = find_supplier_name(text)
    line_items_raw = extract_candidate_line_items(text)
    description = None

    if line_items_raw:
        description = summarise_line_items_rule_based(line_items_raw)
        ai_desc = summarise_line_items_with_openai(line_items_raw, openai_api_key) if openai_api_key else None
        if ai_desc:
            description = ai_desc

    if not description:
        description = limit_to_20_words(text) if text else "Unreadable invoice page"

    return {
        "supplier_name": supplier_name,
        "invoice_number": None,
        "invoice_date": None,
        "description": description,
        "line_items_raw": line_items_raw,
        "net_amount": None,
        "vat_amount": None,
        "total_amount": None,
        "currency": "EUR",
        "tax_code": None,
        "confidence_score": 0.10 if text else 0.0,
        "validation_status": "review",
        "review_required": True,
        "header_raw": "\n".join(text.splitlines()[:12]),
        "totals_raw": extract_totals_region(text),
        "page_text_raw": text,
    }


def extract_rows_from_pdf(pdf_path: str | Path, openai_api_key: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pages = extract_pdf_pages(pdf_path)

    for page in pages:
        text = page["text"] or ""
        row = regex_extract(text, openai_api_key=openai_api_key)

        if count_meaningful_chars(text) > 8 and not any([
            row.get("supplier_name"),
            row.get("invoice_number"),
            row.get("invoice_date"),
            row.get("line_items_raw"),
            row.get("net_amount") is not None,
            row.get("vat_amount") is not None,
            row.get("total_amount") is not None,
        ]):
            row = make_fallback_row(text, openai_api_key=openai_api_key)

        row["page_no"] = page["page_no"]
        row["method_used"] = page["text_source"]
        row["ocr_error"] = page.get("ocr_error")

        if count_meaningful_chars(row.get("page_text_raw") or "") > 0:
            rows.append(row)

    return rows


def process_pdf(pdf_path: str | Path, openai_api_key: str | None = None) -> list[dict[str, Any]]:
    return extract_rows_from_pdf(pdf_path, openai_api_key=openai_api_key)
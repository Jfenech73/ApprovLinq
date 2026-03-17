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
    raw = str(value).strip()
    for sym in ("€", "£", "EUR", "GBP", "USD", "$", " "):
        raw = raw.replace(sym, "")
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


def get_pdf_page_count(pdf_path: str | Path) -> int:
    doc = fitz.open(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def extract_native_pdf_page(pdf_path: str | Path, page_index: int) -> str:
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_index]
        blocks = page.get_text("blocks")
        blocks = sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))
        text = "\n".join(b[4].strip() for b in blocks if len(b) > 4 and b[4].strip())
        return clean_text(text)
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


def bad_supplier_line(line: str) -> bool:
    line_l = line.lower().strip()

    if len(line_l) < 3:
        return True

    skip_patterns = [
        r"invoice",
        r"\binv\b",
        r"tax",
        r"vat",
        r"\bdate\b",
        r"\bpage\b",
        r"customer",
        r"bill to",
        r"ship to",
        r"amount due",
        r"balance due",
        r"total",
        r"subtotal",
        r"iban",
        r"swift",
        r"bic",
        r"email",
        r"www\.",
        r"http",
        r"tel",
        r"phone",
    ]
    if any(re.search(p, line_l, re.I) for p in skip_patterns):
        return True

    # Skip numeric-heavy lines / addresses / VAT numbers
    digits = len(re.findall(r"\d", line_l))
    letters = len(re.findall(r"[a-zA-Z]", line_l))
    if digits > letters:
        return True

    if len(line_l) > 90:
        return True

    return False


def suspicious_supplier_name(value: str | None) -> bool:
    if not value:
        return True

    v = str(value).strip()
    vl = v.lower()

    bad_exact = {
        "invoice",
        "tax invoice",
        "invoice number",
        "invoice no",
        "date",
        "customer",
        "bill to",
        "ship to",
        "total",
        "subtotal",
        "amount due",
        "balance due",
        "vat",
        "tax",
        "page",
        "description",
    }
    if vl in bad_exact:
        return True

    if len(v) < 4:
        return True

    digits = len(re.findall(r"\d", v))
    letters = len(re.findall(r"[A-Za-z]", v))
    if digits >= letters:
        return True

    bad_patterns = [
        r"invoice",
        r"\bdate\b",
        r"\bpage\b",
        r"\btotal\b",
        r"\bsubtotal\b",
        r"\bamount due\b",
        r"\bbalance due\b",
        r"\bvat\b",
        r"\btax\b",
        r"\biban\b",
        r"\bswift\b",
        r"\bbic\b",
        r"\bemail\b",
        r"\bwww\.",
        r"\bhttp",
        r"\btel\b",
        r"\bphone\b",
        r"\bcustomer\b",
        r"\bbill to\b",
        r"\bship to\b",
    ]
    if any(re.search(p, vl, re.I) for p in bad_patterns):
        return True

    return False


def find_supplier_name(text: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # Identify indices that belong to the customer/bill-to section so we never
    # pick the recipient name as the supplier.
    customer_section_indices: set[int] = set()
    customer_label_patterns = [
        r"^\s*bill\s+to\s*[:\-]?\s*$",
        r"^\s*invoice\s+to\s*[:\-]?\s*$",
        r"^\s*sold\s+to\s*[:\-]?\s*$",
        r"^\s*ship\s+to\s*[:\-]?\s*$",
        r"^\s*deliver(?:y)?\s+to\s*[:\-]?\s*$",
        r"^\s*to\s*:\s*$",
        r"^\s*attention\s*[:\-]?\s*$",
        r"^\s*customer\s*[:\-]?\s*$",
        r"^\s*client\s*[:\-]?\s*$",
        r"bill\s+to\s*[:\-]",
        r"invoice\s+to\s*[:\-]",
        r"sold\s+to\s*[:\-]",
        r"ship\s+to\s*[:\-]",
    ]
    for i, line in enumerate(lines):
        for pat in customer_label_patterns:
            if re.search(pat, line, re.I):
                # Mark this line and the next 3 lines as customer section
                for j in range(i, min(i + 4, len(lines))):
                    customer_section_indices.add(j)
                break

    header_lines = lines[:18]
    candidates: list[str] = []

    for i, line in enumerate(header_lines):
        if i in customer_section_indices:
            continue
        if bad_supplier_line(line):
            continue
        candidates.append(line)

    if not candidates:
        return None

    scored: list[tuple[int, str]] = []

    for line in candidates:
        score = 0

        if re.fullmatch(r"[A-Z0-9 &().,\-'/]+", line) and len(line) >= 4:
            score += 3

        if re.search(r"\b(ltd|limited|plc|llc|inc|co\.?|company|services|trading|holdings|group)\b", line, re.I):
            score += 3

        if 4 <= len(line) <= 45:
            score += 2

        if not suspicious_supplier_name(line):
            score += 3

        scored.append((score, line))

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1] if scored else None
    return best[:200] if best else None


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
        r"\bqty\b",
        r"\bquantity\b",
        r"\bunit price\b",
        r"\bdiscount\b",
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
        return limit_to_20_words(" ".join(lines[:2]))

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
                "max_output_tokens": 80,
            },
            timeout=30,
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


def simple_extract(text: str, openai_api_key: str | None = None) -> dict[str, Any]:
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

    _curr = r"(?:EUR|GBP|USD|€|£|\$)?"
    net_raw = first_match([
        rf"(?:subtotal|sub total|net amount|amount excl(?:uding)? vat|taxable amount)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)"
    ], text)
    vat_raw = first_match([
        rf"(?:vat|tax|iva)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)"
    ], text)
    total_raw = first_match([
        rf"(?:amount due|balance due|grand total|total due|total amount|total)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)"
    ], text)

    net_amount = parse_amount(net_raw)
    vat_amount = parse_amount(vat_raw)
    total_amount = parse_amount(total_raw)

    supplier_name = find_supplier_name(text)
    line_items_raw = extract_candidate_line_items(text)

    description = None
    if line_items_raw:
        description = summarise_line_items_rule_based(line_items_raw)
        if openai_api_key:
            ai_desc = summarise_line_items_with_openai(
                line_items_raw,
                openai_api_key,
                model=settings.openai_model,
            )
            if ai_desc:
                description = ai_desc

    if not description:
        description = "Invoice goods or services"

    return {
        "supplier_name": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "description": description,
        "line_items_raw": line_items_raw,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": (
            "GBP" if ("£" in text or "gbp" in text.lower()) else
            "EUR" if ("€" in text or "eur" in text.lower()) else
            "USD" if ("$" in text or "usd" in text.lower()) else
            None
        ),
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
        "- supplier_name is the company ISSUING the invoice (the seller/vendor whose letterhead or logo appears at the top). "
        "It is NOT the customer, buyer, or recipient. The buyer often appears after labels like 'Bill To:', 'Invoice To:', 'To:', or 'Sold To:' — do NOT use that name as supplier_name.\n"
        "- Use null when unknown.\n"
        "- Do not guess.\n"
        "- invoice_date should be DD/MM/YYYY when present.\n"
        "- amounts should be plain numbers with no currency symbols.\n"
        "- currency should be the ISO code e.g. GBP, EUR, USD.\n"
        "- description max 20 words summarising the goods or services purchased.\n\n"
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

    if suspicious_supplier_name(extracted.get("supplier_name")):
        return True
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

    # Always prefer AI supplier_name when AI returns a valid one — the rule-based
    # heuristic cannot reliably distinguish the supplier (invoice sender) from the
    # customer (invoice recipient) when both look like legitimate company names.
    if ai.get("supplier_name") and not suspicious_supplier_name(ai.get("supplier_name")):
        merged["supplier_name"] = ai["supplier_name"]
    elif suspicious_supplier_name(merged.get("supplier_name")) and ai.get("supplier_name"):
        merged["supplier_name"] = ai["supplier_name"]

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

    if merged.get("description") in (None, "", "Invoice extraction", "Invoice goods or services") and ai.get("description"):
        merged["description"] = ai.get("description")

    return merged


def process_pdf_page(
    pdf_path: str | Path,
    page_index: int,
    openai_api_key: str | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)
    native_text = extract_native_pdf_page(pdf_path, page_index)
    ocr_backend = get_ocr_backend()

    final_text = native_text
    method = "native_text"
    ocr_error = None

    if count_meaningful_chars(native_text) < 80 and ocr_backend is not None:
        try:
            ocr_text = clean_text(
                ocr_backend.extract_text_from_pdf_page(pdf_path, page_index, scale=1.8)
            )
            if count_meaningful_chars(ocr_text) > count_meaningful_chars(native_text):
                final_text = ocr_text
                method = f"ocr_{ocr_backend.name}"
        except Exception as e:
            ocr_error = str(e)

    if count_meaningful_chars(final_text) == 0:
        final_text = f"OCR/NATIVE TEXT EMPTY. OCR_ERROR={ocr_error or 'none'}"
        method = f"{method}_empty"

    extracted = simple_extract(final_text, openai_api_key=openai_api_key)

    if settings.use_openai and openai_api_key and count_meaningful_chars(final_text) >= 20:
        # Always call OpenAI when enabled — the rule-based heuristic cannot
        # reliably tell supplier from customer, so we always defer to the AI
        # for supplier_name and fill in any other missing fields.
        ai_fields = openai_extract_invoice_fields(
            final_text,
            openai_api_key,
            model=settings.openai_model,
        )
        extracted = merge_ai_fields(extracted, ai_fields)
        method = f"{method}+openai"

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
        "page_no": page_index + 1,
        "method_used": method,
        "confidence_score": confidence,
        "validation_status": "ok" if confidence >= 0.70 else "review",
        "review_required": confidence < 0.70,
        "header_raw": "\n".join(final_text.splitlines()[:12]),
        "totals_raw": "\n".join(final_text.splitlines()[-10:]) if final_text else None,
        "page_text_raw": final_text[:20000],
    })
    return extracted



def _line_amount_from_text(line: str) -> float | None:
    matches = re.findall(r"(?<!\d)(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", line or "")
    if not matches:
        return None
    return parse_amount(matches[-1])


def split_line_item_rows(page_result: dict[str, Any], tolerance: float = 0.05) -> list[dict[str, Any]]:
    raw = page_result.get("line_items_raw") or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return [page_result]

    rows: list[dict[str, Any]] = []
    summed_total = 0.0
    counted = 0
    for idx, line in enumerate(lines, start=1):
        line_total = _line_amount_from_text(line)
        if line_total is not None:
            summed_total += line_total
            counted += 1
        row = dict(page_result)
        row["description"] = limit_to_20_words(line) or page_result.get("description")
        row["line_items_raw"] = line
        row["line_no"] = idx
        if line_total is not None:
            row["total_amount"] = line_total
            row["net_amount"] = line_total
            row["vat_amount"] = 0.0
        rows.append(row)

    invoice_total = page_result.get("total_amount")
    mismatch = False
    if invoice_total is not None and counted > 0:
        mismatch = abs(float(invoice_total) - float(summed_total)) > tolerance

    for row in rows:
        if mismatch:
            row["review_required"] = True
            row["validation_status"] = "review"
            row["description"] = f"{row.get('description') or 'Invoice line'}"
    return rows


def openai_extract_line_items(
    page_text: str,
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> list[dict[str, Any]] | None:
    """Ask OpenAI to return individual invoice line items as a JSON array.

    Each element: {description, quantity, unit_price, amount}
    Totals, VAT, subtotal rows are excluded by instruction.
    """
    if not api_key or not page_text.strip():
        return None

    prompt = (
        "Extract every individual line item from this invoice page.\n"
        "Return a JSON array only — no other text. Each element must have:\n"
        '  "description": what the item or service is (max 15 words, plain English)\n'
        '  "quantity": numeric quantity or null if not shown\n'
        '  "unit_price": price per unit as a plain number or null if not shown\n'
        '  "amount": this line\'s total as a plain number (no currency symbols)\n'
        "Rules:\n"
        "- Exclude totals, subtotals, VAT, tax, discount summary rows, and shipping/handling rows\n"
        "- Only include actual goods or service lines\n"
        "- If quantity or unit_price are absent from the invoice, use null\n"
        "- Return [] if no line items can be identified\n"
        "- Return the JSON array only, nothing else\n\n"
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
                "max_output_tokens": 600,
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

        raw = " ".join(text_parts).strip()
        if not raw:
            return None

        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0) if m else raw)
        if isinstance(items, list) and items:
            return items
    except Exception:
        return None

    return None


def _build_rows_from_ai_items(
    page_result: dict[str, Any],
    ai_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one InvoiceRow dict per AI-extracted line item.

    Flags for review if the sum of line amounts diverges from the invoice total.
    """
    rows: list[dict[str, Any]] = []
    summed = 0.0

    for idx, item in enumerate(ai_items, start=1):
        row = dict(page_result)

        desc = (item.get("description") or "").strip()
        row["description"] = limit_to_20_words(desc) or page_result.get("description") or "Invoice line"

        # Build a readable line_items_raw from the structured item
        parts = [row["description"]]
        if item.get("quantity") is not None:
            parts.append(f"Qty: {item['quantity']}")
        if item.get("unit_price") is not None:
            parts.append(f"@ {item['unit_price']}")
        if item.get("amount") is not None:
            parts.append(f"= {item['amount']}")
        row["line_items_raw"] = "  ".join(parts)

        amount = None
        raw_amt = item.get("amount")
        if raw_amt is not None:
            try:
                amount = float(raw_amt)
            except (TypeError, ValueError):
                amount = parse_amount(str(raw_amt))

        if amount is not None:
            summed += amount
            row["total_amount"] = amount
            row["net_amount"] = amount
            row["vat_amount"] = 0.0

        row["line_no"] = idx
        rows.append(row)

    if not rows:
        return [page_result]

    # Cross-check against the invoice-level total
    invoice_total = page_result.get("total_amount")
    if invoice_total is not None and abs(float(invoice_total) - summed) > 0.10:
        for row in rows:
            row["review_required"] = True
            row["validation_status"] = "review"

    return rows


def process_pdf_page_rows(
    pdf_path: str | Path,
    page_index: int,
    scan_mode: str = "summary",
    openai_api_key: str | None = None,
) -> list[dict[str, Any]]:
    page_result = process_pdf_page(pdf_path, page_index=page_index, openai_api_key=openai_api_key)

    if (scan_mode or "summary").lower() == "lines":
        # Prefer AI-structured line items; fall back to rule-based splitter
        if settings.use_openai and openai_api_key:
            page_text = page_result.get("page_text_raw") or ""
            ai_items = openai_extract_line_items(page_text, openai_api_key, model=settings.openai_model)
            if ai_items:
                return _build_rows_from_ai_items(page_result, ai_items)
        return split_line_item_rows(page_result)

    return [page_result]


def process_pdf(pdf_path: str | Path, openai_api_key: str | None = None) -> list[dict[str, Any]]:
    page_count = get_pdf_page_count(pdf_path)
    return [
        process_pdf_page(pdf_path, page_index=i, openai_api_key=openai_api_key)
        for i in range(page_count)
    ]
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

from app.services.ocr import OCRBackend, OCRSpaceBackend, PaddleOCRBackend
from app.config import settings

logger = logging.getLogger(__name__)


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
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
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
    bad = {
        "to", "from", "date", "invoice", "invoice no", "invoice number", "page",
        "details", "copy", "original", "number", "no", "ref", "reference",
        "involce", "invoce", "invoiice",
    }
    if v in bad:
        return True
    if len(v) < 3:
        return True
    # Pure-letter strings (no digits) are never real invoice numbers
    if re.match(r"^[A-Za-z\s]+$", v):
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


_GENERIC_CORP_WORDS = frozenset({
    "LTD", "LIMITED", "PLC", "LLC", "INC", "CO", "CORP", "COMPANY",
    "THE", "AND", "OF", "FOR", "A", "PRIVATE", "PUBLIC", "GROUP",
})


def _build_account_tokens(company_name: str | None) -> frozenset[str]:
    """Return uppercase word tokens from the company name that are long and
    distinctive enough to uniquely identify the account holder.  Used as a
    hard blacklist: any supplier candidate that contains one of these tokens
    is rejected — it is the customer, not the supplier.
    """
    if not company_name:
        return frozenset()
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z]+", company_name):
        w = word.upper()
        if len(w) >= 4 and w not in _GENERIC_CORP_WORDS:
            tokens.add(w)
    return frozenset(tokens)


def _find_supplier_from_contact_block(
    lines: list[str],
    account_tokens: frozenset[str],
) -> str | None:
    """Anchor the supplier name using the letterhead contact block.

    Real suppliers always publish their Tel/Fax/Email/VAT with actual *values*
    (not just labels) in consecutive lines.  The last non-address company-like
    line before that block is the supplier.

    This is especially effective for two-column OCR layouts where the customer
    name appears at the top and the supplier's contact block appears lower.
    """
    # Patterns that contain actual values (phone digits, @ sign, MT+digits…)
    contact_value_patterns = [
        r"(?:tel|fax|phone)\s*[:\+]?\s*[\+\(]?\d{5,}",
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
        r"\bvat\s+(?:no\.?|number|reg(?:\.|\s)?no\.?)\s*[:\-]?\s*[A-Z]{2}\d",
        r"\bvat\s*no\s*[:\-]?\s*[A-Z]{2}\d",
        r"\bvat\s*reg(?:\.|\s+)no\s*[:\-]?\s*[A-Z]",
        r"MT\d{6,}",               # Malta VAT number format
        r"IE\d{6,}[A-Z]",          # Irish VAT
        r"\bBCRS\b",               # Malta BCRS registration
        r"\bEXO\s+\d{4}",         # EXO number (Malta)
    ]

    # Address lines to skip when walking backward — deliberately strict to
    # avoid false-positives on company names that contain city words (e.g.
    # "Azzopardi Gzira Fish Shop").  Only short pure-city lines or lines that
    # start with a street number are excluded here.
    address_patterns = [
        r"\bstreet\b", r"\broad\b", r"\bave(?:nue)?\b", r"\bfloor\b",
        r"\bsuite\b", r"\bbuilding\b", r"\bindustrial\s+park\b",
        r"\btriq\b",                # Maltese word for "street"
        r"\bdistrict\b",            # e.g. "Central Business District"
        r"\bzone\s+\d",             # e.g. "Zone 3"
        r"^\d+[,/\s]",              # starts with street number
        r"^[A-Z]{2,3}\s?\d{4,}$",  # postcode-only lines like "SLM 1856"
        r"\b[A-Z]{2,3}\s?\d{4,}\b",# inline postcode like "STJ1017", "SLM 1856", "SLM1856"
    ]

    def _is_plausible_company(candidate: str) -> bool:
        """Return True only if the line looks like a real company name."""
        if not candidate or len(candidate) < 4:
            return False
        if bad_supplier_line(candidate):
            return False
        if suspicious_supplier_name(candidate):
            return False
        if any(re.search(p, candidate, re.I) for p in address_patterns):
            return False
        stripped = candidate.strip()
        words = stripped.split()
        # Reject short 1-2 word lines that look like a city or town name
        if len(words) <= 2 and len(stripped) <= 18:
            if not re.search(
                r"\b(shop|store|market|imports?|exports?|foods?|supplies|ltd|limited"
                r"|brothers?|group|corp|company|services|trading|fish|wine|spirits|meats?)\b",
                stripped, re.I,
            ):
                return False
        # Reject account name tokens
        if account_tokens:
            line_words = set(re.findall(r"[A-Z]{4,}", candidate.upper()))
            if line_words & account_tokens:
                return False
        return True

    # Find ALL lines with actual contact values (not just the first).
    # We try each one in order and return the first backward-scan result that
    # produces a plausible company name — this lets us skip customer address
    # blocks that happen to contain a VAT/phone number before the supplier block.
    contact_indices: list[int] = []
    for i, line in enumerate(lines):
        if any(re.search(p, line, re.I) for p in contact_value_patterns):
            contact_indices.append(i)
            if len(contact_indices) >= 4:
                break

    for contact_idx in contact_indices:
        if contact_idx < 1:
            continue
        # Walk backward up to 10 lines looking for a plausible company name
        for i in range(contact_idx - 1, max(-1, contact_idx - 10), -1):
            candidate = lines[i]
            if _is_plausible_company(candidate):
                return candidate
        # If backward scan from this contact block found nothing, try the next block

    return None


def find_supplier_name(
    text: str,
    account_tokens: frozenset[str] = frozenset(),
) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # ------------------------------------------------------------------ #
    # Step 1: Pre-scan the ENTIRE text to discover customer company names. #
    # Combines the caller-supplied account tokens with any tokens found    #
    # in "Account X" / "Account Name: X" patterns inside the document.    #
    # ------------------------------------------------------------------ #
    customer_name_tokens: set[str] = set(account_tokens)
    # "Account NAAR" / "Account Name: NAAR LTD" patterns
    for m in re.finditer(r"\bAccount\s+([A-Z][A-Za-z0-9]+)", text):
        customer_name_tokens.add(m.group(1).strip().upper())
    # "Account Name: NAAR LTD"
    for m in re.finditer(r"\bAccount\s+Name\s*[:\-]\s*([A-Z][A-Za-z0-9 ]+)", text, re.I):
        for tok in m.group(1).strip().upper().split():
            if len(tok) >= 4:
                customer_name_tokens.add(tok)
    frozen_customer_tokens: frozenset[str] = frozenset(customer_name_tokens)

    # ------------------------------------------------------------------ #
    # Step 2: Identify customer/bill-to section by explicit labels.        #
    # ------------------------------------------------------------------ #
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
        # Receipt/POS-style markers
        r"client\s+details",
        r"client\s+code\s*[:\-]",
        r"cashier\s*[:\-]",
    ]
    for i, line in enumerate(lines):
        for pat in customer_label_patterns:
            if re.search(pat, line, re.I):
                # Mark this line and the next 4 lines as customer section
                for j in range(i, min(i + 5, len(lines))):
                    customer_section_indices.add(j)
                break

    # ------------------------------------------------------------------ #
    # Step 3: Combine adjacent short lines at the very top.               #
    # OCR of two-column layouts often reads the supplier letterhead as    #
    # two short lines ("Apple Cores" / "FOODS") that together form one    #
    # company name.  Join them so they can be scored as a unit.          #
    # ------------------------------------------------------------------ #
    header_lines = lines[:18]
    effective_lines: list[tuple[int, str]] = []  # (first_original_pos, text)

    skip_next = False
    for i, line in enumerate(header_lines):
        if skip_next:
            skip_next = False
            continue
        next_line = header_lines[i + 1] if i + 1 < len(header_lines) else ""
        # Try to join with the next line if both are short and look like name tokens
        # Do NOT join if the next line looks like an address or a standalone city/town.
        next_is_address = bool(
            re.search(
                r"\bstreet\b|\broad\b|\bave(?:nue)?\b|\bfloor\b|\bsuite\b"
                r"|\btriq\b|\bdistrict\b|\bzone\s+\d",
                next_line, re.I,
            )
            or re.match(r"^\d+[,/\s]", next_line)
            or re.search(r"\b[A-Z]{2,3}\s?\d{4,}\b", next_line)
        )
        if (
            i + 1 < len(header_lines)
            and i not in customer_section_indices
            and (i + 1) not in customer_section_indices
            and len(line) <= 20
            and len(next_line) <= 20
            and not bad_supplier_line(line)
            and not bad_supplier_line(next_line)
            and not next_is_address
            and re.fullmatch(r"[A-Za-z0-9 &().,\-'/]+", line)
            and re.fullmatch(r"[A-Za-z0-9 &().,\-'/]+", next_line)
        ):
            combined = f"{line} {header_lines[i + 1]}"
            effective_lines.append((i, combined))
            skip_next = True
        else:
            effective_lines.append((i, line))

    # ------------------------------------------------------------------ #
    # Step 3b: Contact-block anchor — find the supplier by anchoring to  #
    # the first actual Tel/Fax/Email/VAT VALUE line and looking backward. #
    # This is the strongest signal and overrides heuristic scoring when   #
    # it returns a result that doesn't match the account name.            #
    # ------------------------------------------------------------------ #
    contact_anchor = _find_supplier_from_contact_block(lines, frozen_customer_tokens)

    candidates: list[tuple[int, str]] = []
    for pos, line in effective_lines:
        if pos in customer_section_indices:
            continue
        if bad_supplier_line(line):
            continue
        candidates.append((pos, line))

    if not candidates and contact_anchor is None:
        return None

    scored: list[tuple[int, str]] = []

    for pos, line in candidates:
        score = 0

        # Hard-reject any candidate that shares a distinctive token with the
        # known account company name — this covers all NAAR / NAAR LTD /
        # NAAR RESTOBAR / NAAR RESTAURANT variants in one shot.
        line_words = set(re.findall(r"[A-Za-z]{4,}", line.upper()))
        if line_words & frozen_customer_tokens:
            continue  # Hard exclusion — never the supplier

        # Strong positional bias: top of the document is the supplier letterhead.
        if pos == 0:
            score += 8
        elif pos <= 2:
            score += 4
        elif pos <= 5:
            score += 2

        # All-caps company name bonus
        if re.fullmatch(r"[A-Z0-9 &().,\-'/]+", line) and len(line) >= 4:
            score += 3

        # Corporate entity suffix — broad set including food/trade terms
        if re.search(
            r"\b(ltd|limited|plc|llc|inc|co\.?|company|services|trading|holdings|group"
            r"|foods?|supplies|distribution|imports?|exports?|catering|enterprises?|corp"
            r"|brothers?|sisters?|partners?|associates?)\b",
            line, re.I,
        ):
            score += 2

        if 4 <= len(line) <= 60:
            score += 2

        if not suspicious_supplier_name(line):
            score += 3

        scored.append((score, line))

    if not scored and contact_anchor is None:
        return None

    # Pick the best heuristic candidate
    scored.sort(key=lambda x: x[0], reverse=True)
    heuristic_best = scored[0][1] if scored else None

    # If the contact-block anchor found a name, prefer it over the heuristic
    # result UNLESS the heuristic found something with a much higher score
    # (i.e. a clear letterhead name at position 0).
    if contact_anchor:
        contact_words = set(re.findall(r"[A-Za-z]{4,}", contact_anchor.upper()))
        # Discard the anchor if it still matches the account name
        if contact_words & frozen_customer_tokens:
            contact_anchor = None

    if contact_anchor and heuristic_best:
        # If they agree (one is a substring of the other), use the longer one
        if (contact_anchor.upper() in heuristic_best.upper() or
                heuristic_best.upper() in contact_anchor.upper()):
            best = heuristic_best if len(heuristic_best) >= len(contact_anchor) else contact_anchor
        else:
            # They disagree — prefer the heuristic result ONLY if it has
            # a very high score (position 0 with ≥12 points)
            top_score = scored[0][0] if scored else 0
            best = heuristic_best if top_score >= 12 else contact_anchor
    elif contact_anchor:
        best = contact_anchor
    else:
        best = heuristic_best

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


def normalise_company_name(name: str | None) -> str | None:
    """Preserve the original casing when any uppercase letters exist.
    If the entire name is lowercase (e.g. returned by vision AI reading a
    stylised font), promote it to title case so it displays correctly.
    Short words like 'and', 'of', 'for', 'the' stay lowercase mid-name.
    """
    if not name:
        return name
    name = name.strip()
    if not name:
        return name
    # If ANY uppercase letter already present, trust the source.
    if any(c.isupper() for c in name):
        return name
    # All-lowercase — apply title case with common small-word exceptions.
    _lower_words = {"and", "or", "of", "for", "the", "a", "an", "in", "on",
                    "at", "by", "to", "with", "from", "&"}
    words = name.split()
    result = []
    for i, w in enumerate(words):
        if i == 0 or w.lower() not in _lower_words:
            result.append(w.capitalize())
        else:
            result.append(w.lower())
    return " ".join(result)


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
        "- No supplier names, invoice numbers, or amounts\n"
        "- Summarise the goods or services purchased\n"
        "- Return only the description text, nothing else\n\n"
        f"Invoice item lines:\n{line_items_text}"
    )

    raw = _call_openai(prompt, api_key, model, max_tokens=80, timeout=30)
    if raw:
        return limit_to_20_words(raw)
    return None


def simple_extract(
    text: str,
    openai_api_key: str | None = None,
    account_company_name: str | None = None,
) -> dict[str, Any]:
    account_tokens = _build_account_tokens(account_company_name)

    invoice_number = first_match([
        # Standard label patterns — allow period before colon ("No.: 45005")
        r"invoice\s*(?:no\.?|number|#|nr\.?)\s*[.:\-]*\s*([A-Z0-9][A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
        r"invoice\s*(?:no\.?|number|#|nr\.?)\s*[.:\-]*\s*([0-9][A-Z0-9\/\-_]*)",
        # "INV" prefix followed immediately by digits
        r"\bINV[.\-_]?([0-9][A-Z0-9\/\-_]*)",
        # Generic document number
        r"\bdocument\s*(?:no\.?|number|nr\.?)\s*[.:\-]*\s*([A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
        # Fallback: "inv" word-boundary only when followed by colon/dash then a number
        r"\binv(?:oice|oiice|oice)?\s*[.:\-]+\s*([A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
    ], text)
    # Reject anything that looks like a word rather than a number
    if suspicious_invoice_number(invoice_number):
        invoice_number = None

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

    supplier_name = find_supplier_name(text, account_tokens=account_tokens)
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


def _call_openai(prompt: str, api_key: str, model: str, max_tokens: int, timeout: int = 45) -> str | None:
    """Shared helper — POST to OpenAI Responses API and return raw text output."""
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "input": prompt, "max_output_tokens": max_tokens},
            timeout=timeout,
        )
        response.raise_for_status()
        parts = []
        for item in response.json().get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    txt = content.get("text", "")
                    if txt:
                        parts.append(txt)
        return " ".join(parts).strip() or None
    except Exception as exc:
        logger.warning("_call_openai failed: %s", exc)
        return None


def render_page_for_vision(
    pdf_path: Path,
    page_index: int,
    scale: float = 1.5,
    quality: int = 80,
) -> str | None:
    """Render a PDF page to a base64-encoded JPEG string for vision model input.

    Returns None if rendering fails so the caller can fall back to text-only.
    Uses pypdfium2 via the existing OCRBackend render helper.
    """
    try:
        jpeg_bytes = OCRBackend.render_pdf_page_to_jpeg_bytes(
            pdf_path, page_index, scale=scale, quality=quality
        )
        if not jpeg_bytes:
            return None
        # Hard cap: OpenAI refuses payloads > ~20 MB; keep well under that
        if len(jpeg_bytes) > 4 * 1024 * 1024:
            # Try again at lower quality
            jpeg_bytes = OCRBackend.render_pdf_page_to_jpeg_bytes(
                pdf_path, page_index, scale=1.0, quality=60
            )
        return base64.b64encode(jpeg_bytes).decode("ascii") if jpeg_bytes else None
    except Exception as exc:
        logger.warning("render_page_for_vision failed: %s", exc)
        return None


def openai_extract_invoice_vision(
    jpeg_b64: str,
    page_text: str,
    api_key: str,
    model: str = "gpt-4.1-mini",
    account_company_name: str | None = None,
) -> dict[str, Any] | None:
    """Stage 3 (vision path) — multimodal extraction using the page image.

    Sends the rendered JPEG alongside the OCR text so the AI can use visual
    layout cues (column positions, font sizes, letterhead vs body, table
    borders) in addition to the raw text — far more reliable than text alone
    on scanned, two-column, or complex invoice layouts.

    The prompt and output schema are identical to openai_extract_invoice_fields
    so the result slots directly into merge_ai_fields with no changes.
    """
    if not api_key or not jpeg_b64:
        return None

    account_rule = ""
    if account_company_name:
        account_rule = (
            f"  * CRITICAL: '{account_company_name}' is the BUYER scanning these invoices.\n"
            f"    Any variant (abbreviated, different suffix, OCR typo) is ALWAYS the customer.\n"
            f"    NEVER assign '{account_company_name}' or any of its variants as supplier.name.\n"
        )

    text_block = (
        f"OCR TEXT (may have noise):\n{page_text[:8000]}\n\n" if page_text.strip() else ""
    )

    system_prompt = (
        "You are an expert invoice extraction engine with full document understanding.\n\n"
        "You are given BOTH the visual image of the invoice page AND its OCR text.\n"
        "Use the IMAGE as the primary source — it preserves layout, columns, font sizes,\n"
        "table structure, and visual hierarchy. Use the OCR text to resolve any unclear\n"
        "characters in the image.\n\n"
        "OBJECTIVE:\n"
        "Extract structured invoice data with maximum precision using:\n"
        "- Visual layout: letterhead position, column separation, section borders\n"
        "- Font hierarchy: larger/bolder text = company name / section headings\n"
        "- Table structure: item rows, qty, price, subtotal/VAT/total rows\n"
        "- Label–value pairing: 'Invoice No:', 'Date:', 'VAT No:', etc.\n\n"
        "RULES:\n"
        "- Do not guess. Return null for any field you cannot determine with confidence.\n"
        "- Preserve original text for names and identifiers (no paraphrasing).\n"
        "- Normalize dates to YYYY-MM-DD.\n"
        "- Normalize amounts as plain decimal numbers (no symbols or commas).\n"
        "- NEVER confuse supplier and customer.\n"
        "- Never invent line items or amounts.\n\n"
        "SUPPLIER vs CUSTOMER:\n"
        "- Supplier (issuer/seller): name in the TOP SECTION / LETTERHEAD of the document.\n"
        "  * Usually large bold text, accompanied by address, phone, email, VAT number.\n"
        "  * NEVER follows buyer labels: 'Bill To', 'Invoice To', 'To:', 'Customer:',\n"
        "    'Client:', 'Attention:', 'Account Name:', 'Account Ref:', 'Sold To', 'Ship To'.\n"
        f"{account_rule}"
        "- Customer (recipient/buyer): typically in a labelled section below the letterhead.\n\n"
        "LINE ITEMS:\n"
        "- Extract individual goods/service rows from the table only.\n"
        "- Exclude totals, subtotals, VAT summary rows, and discounts.\n\n"
        "TOTALS VALIDATION:\n"
        "- Check: subtotal + tax_total ≈ gross_total.\n"
        "- Set totals_reconcile = true/false accordingly.\n\n"
        "CONFIDENCE: Rate each section 0.0–1.0 based on clarity in the image.\n"
        "If a section is clearly printed and unambiguous → 0.9–1.0.\n"
        "If OCR noise or partial obscuring → 0.5–0.8. If not found → 0.0.\n\n"
        "OUTPUT — return strict JSON only, no other text:\n"
        "{\n"
        '  "document_type": "invoice|credit_note|unknown",\n'
        '  "extraction_status": "complete|partial|review_required",\n'
        '  "supplier": {"name":null,"address":null,"vat_number":null,"email":null,"phone":null,"confidence":0.0},\n'
        '  "customer": {"name":null,"address":null,"vat_number":null,"confidence":0.0},\n'
        '  "invoice_header": {"invoice_number":null,"invoice_date":null,"due_date":null,"currency":null},\n'
        '  "description": null,\n'
        '  "line_items": [{"description":null,"quantity":null,"unit_price":null,"net_amount":null}],\n'
        '  "totals": {"subtotal":null,"tax_total":null,"gross_total":null,"amount_due":null,"confidence":0.0},\n'
        '  "validation": {"totals_reconcile":null,"issues":[]},\n'
        '  "confidence": {"supplier":0.0,"customer":0.0,"lines":0.0,"totals":0.0}\n'
        "}\n\n"
        'description: a plain-English summary (max 20 words) of what goods or services were purchased.\n'
        '  * Do NOT include supplier name, invoice number, or amounts.\n'
        '  * Example: "Fresh seafood, vegetables and dry goods" or "Monthly software licence fee"\n'
        '  * If no goods/services are listed, summarise the invoice purpose from context.\n'
    )

    full_prompt = system_prompt + text_block

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": full_prompt},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{jpeg_b64}",
                            },
                        ],
                    }
                ],
                "max_output_tokens": 1200,
            },
            timeout=60,
        )
        response.raise_for_status()
        parts = []
        for item in response.json().get("output", []):
            for content in item.get("content", []):
                if content.get("type") in ("output_text", "text"):
                    txt = content.get("text", "")
                    if txt:
                        parts.append(txt)
        raw = " ".join(parts).strip()
    except Exception as exc:
        logger.warning("openai_extract_invoice_vision API call failed: %s", exc)
        return None

    if not raw:
        return None

    try:
        m = re.search(r"\{.*\}", raw, re.S)
        payload = json.loads(m.group(0) if m else raw)
    except Exception as exc:
        logger.warning("openai_extract_invoice_vision JSON parse failed: %s", exc)
        return None

    # Parse identically to the text-only function — same schema, same field mapping
    supplier = payload.get("supplier") or {}
    customer = payload.get("customer") or {}
    header = payload.get("invoice_header") or {}
    totals = payload.get("totals") or {}
    validation = payload.get("validation") or {}
    confidence_sections = payload.get("confidence") or {}

    def _safe_amount(val: Any) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return parse_amount(str(val))

    # Description: use the top-level field first, fall back to line_items summary
    description: str | None = None
    top_desc = (payload.get("description") or "").strip()
    if top_desc:
        description = limit_to_20_words(top_desc)
    else:
        items = payload.get("line_items") or []
        if items and isinstance(items, list):
            descs = [str(it.get("description") or "").strip() for it in items if it.get("description")]
            if descs:
                description = limit_to_20_words("; ".join(descs))

    return {
        "supplier_name": supplier.get("name"),
        "invoice_number": header.get("invoice_number"),
        "invoice_date": parse_date(header.get("invoice_date")) if header.get("invoice_date") else None,
        "description": description,
        "net_amount": _safe_amount(totals.get("subtotal")),
        "vat_amount": _safe_amount(totals.get("tax_total")),
        "total_amount": _safe_amount(totals.get("gross_total") or totals.get("amount_due")),
        "currency": header.get("currency"),
        "tax_code": None,
        "supplier_address": supplier.get("address"),
        "supplier_vat": supplier.get("vat_number"),
        "supplier_email": supplier.get("email"),
        "supplier_phone": supplier.get("phone"),
        "customer_name": customer.get("name"),
        "customer_address": customer.get("address"),
        "customer_vat": customer.get("vat_number"),
        "due_date": parse_date(header.get("due_date")) if header.get("due_date") else None,
        "document_type": payload.get("document_type"),
        "extraction_status": payload.get("extraction_status"),
        "totals_reconcile": validation.get("totals_reconcile"),
        "ai_issues": validation.get("issues") or [],
        "ai_confidence": {
            "supplier": confidence_sections.get("supplier", 0.0),
            "customer": confidence_sections.get("customer", 0.0),
            "lines": confidence_sections.get("lines", 0.0),
            "totals": confidence_sections.get("totals", 0.0),
        },
    }


def openai_extract_invoice_fields(
    page_text: str,
    api_key: str,
    model: str = "gpt-4.1-mini",
    account_company_name: str | None = None,
) -> dict[str, Any] | None:
    """Stage 3 of the extraction pipeline — full-schema AI extraction.

    Uses the Invoice AI Extraction Framework master prompt to return a
    structured JSON payload covering supplier, customer, invoice header,
    line items, totals, validation, and per-section confidence scores.

    The returned dict maps all framework fields back to the legacy field names
    used by merge_ai_fields / process_pdf_page so the rest of the pipeline
    requires no changes.
    """
    if not api_key or not page_text.strip():
        return None

    account_rule = ""
    if account_company_name:
        account_rule = (
            f"  * CRITICAL: '{account_company_name}' is the BUYER scanning these invoices.\n"
            f"    Any variant (abbreviated, different suffix, OCR typo) is ALWAYS the customer.\n"
            f"    NEVER assign '{account_company_name}' or any of its variants as supplier.name.\n"
        )

    prompt = (
        "You are an expert invoice extraction engine.\n\n"
        "OBJECTIVE:\n"
        "Extract structured invoice data with maximum accuracy using labels, "
        "layout positioning, table structure, and arithmetic validation.\n\n"
        "RULES:\n"
        "- Do not guess. Return null for any field you cannot determine with confidence.\n"
        "- Preserve original text for names and identifiers.\n"
        "- Normalize dates to YYYY-MM-DD.\n"
        "- Normalize amounts as plain decimal numbers (no currency symbols or commas).\n"
        "- Separate supplier vs customer STRICTLY — never confuse them.\n"
        "- Never invent line items or amounts.\n\n"
        "SEGMENT DEFINITIONS:\n"
        "- supplier: the company that ISSUED this invoice (seller/vendor).\n"
        "  * Their name is at the TOP of the document in the letterhead.\n"
        "  * They own the Tel/Fax/Email/VAT contact block in the header.\n"
        "  * NEVER use a name that follows buyer labels: 'Bill To', 'Invoice To',\n"
        "    'To:', 'Customer:', 'Client:', 'Attention:', 'Account Name:', 'Account Ref:',\n"
        "    'Sold To', 'Ship To', 'Deliver To'.\n"
        f"{account_rule}"
        "- customer: the company that RECEIVED this invoice (buyer/purchaser).\n"
        "- invoice_header: invoice number (must contain ≥1 digit), date, due date, currency.\n"
        "  * Reject words like 'Invoice', 'Details', 'Copy' as invoice_number.\n"
        "- line_items: individual goods/service rows only — exclude totals/VAT summary rows.\n"
        "- totals: subtotal (net), tax_total (VAT), gross_total (inc. tax), amount_due.\n"
        "- validation.totals_reconcile: true if subtotal + tax_total ≈ gross_total.\n"
        "- description: a plain-English summary (max 20 words) of what was purchased.\n"
        "  Do NOT include supplier name, invoice number, or amounts.\n\n"
        "CONFIDENCE: Score each section 0.0–1.0 based on clarity of source text.\n\n"
        "OUTPUT — return strict JSON only, no other text:\n"
        "{\n"
        '  "document_type": "invoice|credit_note|unknown",\n'
        '  "extraction_status": "complete|partial|review_required",\n'
        '  "supplier": {"name":null,"address":null,"vat_number":null,"email":null,"phone":null,"confidence":0.0},\n'
        '  "customer": {"name":null,"address":null,"vat_number":null,"confidence":0.0},\n'
        '  "invoice_header": {"invoice_number":null,"invoice_date":null,"due_date":null,"currency":null},\n'
        '  "description": null,\n'
        '  "line_items": [{"description":null,"quantity":null,"unit_price":null,"net_amount":null}],\n'
        '  "totals": {"subtotal":null,"tax_total":null,"gross_total":null,"amount_due":null,"confidence":0.0},\n'
        '  "validation": {"totals_reconcile":null,"issues":[]},\n'
        '  "confidence": {"supplier":0.0,"customer":0.0,"lines":0.0,"totals":0.0}\n'
        "}\n\n"
        f"PAGE TEXT:\n{page_text[:12000]}"
    )

    raw = _call_openai(prompt, api_key, model, max_tokens=900)
    if not raw:
        return None

    try:
        m = re.search(r"\{.*\}", raw, re.S)
        payload = json.loads(m.group(0) if m else raw)
    except Exception as exc:
        logger.warning("openai_extract_invoice_fields JSON parse failed: %s", exc)
        return None

    supplier = payload.get("supplier") or {}
    customer = payload.get("customer") or {}
    header = payload.get("invoice_header") or {}
    totals = payload.get("totals") or {}
    validation = payload.get("validation") or {}
    confidence_sections = payload.get("confidence") or {}

    def _safe_amount(val: Any) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return parse_amount(str(val))

    # Description: use the top-level field first (explicit summary from AI),
    # fall back to joining line item descriptions if the top-level is absent.
    _top_desc = (payload.get("description") or "").strip()
    if _top_desc:
        description: str | None = limit_to_20_words(_top_desc)
    else:
        items = payload.get("line_items") or []
        descs = [str(it.get("description") or "").strip() for it in items if it.get("description")]
        description = limit_to_20_words("; ".join(descs)) if descs else None

    # Map onto the legacy field names expected by merge_ai_fields
    result: dict[str, Any] = {
        # Core fields (legacy names)
        "supplier_name": supplier.get("name"),
        "invoice_number": header.get("invoice_number"),
        "invoice_date": parse_date(header.get("invoice_date")) if header.get("invoice_date") else None,
        "description": description,
        "net_amount": _safe_amount(totals.get("subtotal")),
        "vat_amount": _safe_amount(totals.get("tax_total")),
        "total_amount": _safe_amount(totals.get("gross_total") or totals.get("amount_due")),
        "currency": header.get("currency"),
        "tax_code": None,
        # Extended framework fields (stored in result dict, not persisted to DB)
        "supplier_address": supplier.get("address"),
        "supplier_vat": supplier.get("vat_number"),
        "supplier_email": supplier.get("email"),
        "supplier_phone": supplier.get("phone"),
        "customer_name": customer.get("name"),
        "customer_address": customer.get("address"),
        "customer_vat": customer.get("vat_number"),
        "due_date": parse_date(header.get("due_date")) if header.get("due_date") else None,
        "document_type": payload.get("document_type"),
        "extraction_status": payload.get("extraction_status"),
        "totals_reconcile": validation.get("totals_reconcile"),
        "ai_issues": validation.get("issues") or [],
        "ai_confidence": {
            "supplier": confidence_sections.get("supplier", 0.0),
            "customer": confidence_sections.get("customer", 0.0),
            "lines": confidence_sections.get("lines", 0.0),
            "totals": confidence_sections.get("totals", 0.0),
        },
    }
    return result


def openai_validate_extraction(
    page_text: str,
    extracted: dict[str, Any],
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> dict[str, Any] | None:
    """Stage 4 of the extraction pipeline — second-pass validation.

    Checks supplier/customer correctness, invoice number vs PO confusion,
    date correctness, and totals reconciliation.  Returns a small dict with
    validated_status, issues, and fields_to_review.
    """
    if not api_key:
        return None
    # Only validate when we have enough data to be meaningful
    has_supplier = bool(extracted.get("supplier_name"))
    has_number = bool(extracted.get("invoice_number"))
    has_amounts = extracted.get("total_amount") is not None
    if not (has_supplier or has_number or has_amounts):
        return None

    extracted_summary = json.dumps({
        "supplier_name": extracted.get("supplier_name"),
        "customer_name": extracted.get("customer_name"),
        "invoice_number": extracted.get("invoice_number"),
        "invoice_date": str(extracted.get("invoice_date") or ""),
        "due_date": str(extracted.get("due_date") or ""),
        "net_amount": extracted.get("net_amount"),
        "vat_amount": extracted.get("vat_amount"),
        "total_amount": extracted.get("total_amount"),
        "totals_reconcile": extracted.get("totals_reconcile"),
        "ai_issues": extracted.get("ai_issues") or [],
    }, default=str)

    prompt = (
        "You are an invoice data validator.\n\n"
        "Given the original invoice text and extracted data, check:\n"
        "1. Supplier vs customer assignment is correct (supplier issued, customer received).\n"
        "2. Invoice number is not confused with a PO, delivery, or account reference number.\n"
        "3. Invoice date and due date are not swapped.\n"
        "4. Totals reconcile: net_amount + vat_amount ≈ total_amount (within 0.02).\n"
        "5. VAT numbers are plausible (not confused with invoice numbers).\n\n"
        "RULES:\n"
        "- Do not assume. Flag inconsistencies only.\n"
        "- Be concise. Each issue max 15 words.\n\n"
        "OUTPUT — strict JSON only:\n"
        '{"validated_status":"passed|passed_with_warnings|failed","issues":[],"fields_to_review":[]}\n\n'
        f"EXTRACTED DATA:\n{extracted_summary}\n\n"
        f"ORIGINAL TEXT (first 4000 chars):\n{page_text[:4000]}"
    )

    raw = _call_openai(prompt, api_key, model, max_tokens=250, timeout=30)
    if not raw:
        return None

    try:
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0) if m else raw)
        return {
            "validated_status": result.get("validated_status", "passed"),
            "issues": result.get("issues") or [],
            "fields_to_review": result.get("fields_to_review") or [],
        }
    except Exception as exc:
        logger.warning("openai_validate_extraction JSON parse failed: %s", exc)
        return None


def merge_ai_fields(base: dict[str, Any], ai: dict[str, Any] | None) -> dict[str, Any]:
    """Merge AI-extracted fields into the rule-based base result.

    Core fields follow a "trust AI unless obviously wrong" strategy.
    Extended framework fields (supplier_address, customer_name, etc.) are
    copied across directly — they don't exist in the rule-based result.
    """
    if not ai:
        return base

    merged = dict(base)

    # -- Supplier name ---------------------------------------------------------
    # Always prefer the AI supplier when it returns a valid one; the rule-based
    # heuristic cannot reliably separate supplier from customer in two-column
    # or inverted OCR layouts.
    if ai.get("supplier_name") and not suspicious_supplier_name(ai.get("supplier_name")):
        merged["supplier_name"] = ai["supplier_name"]
    elif suspicious_supplier_name(merged.get("supplier_name")) and ai.get("supplier_name"):
        merged["supplier_name"] = ai["supplier_name"]
    # Normalise casing: promote all-lowercase names to title case.
    merged["supplier_name"] = normalise_company_name(merged.get("supplier_name"))

    # -- Invoice number --------------------------------------------------------
    if suspicious_invoice_number(merged.get("invoice_number")) and ai.get("invoice_number"):
        merged["invoice_number"] = ai.get("invoice_number")

    # -- Date fields -----------------------------------------------------------
    if merged.get("invoice_date") is None and ai.get("invoice_date") is not None:
        merged["invoice_date"] = ai.get("invoice_date")
    # Due date is new — copy from AI whenever present
    if ai.get("due_date"):
        merged["due_date"] = ai.get("due_date")

    # -- Amounts ---------------------------------------------------------------
    if merged.get("net_amount") is None and ai.get("net_amount") is not None:
        merged["net_amount"] = ai.get("net_amount")
    if merged.get("vat_amount") is None and ai.get("vat_amount") is not None:
        merged["vat_amount"] = ai.get("vat_amount")
    if merged.get("total_amount") is None and ai.get("total_amount") is not None:
        merged["total_amount"] = ai.get("total_amount")

    # -- Metadata --------------------------------------------------------------
    if not merged.get("currency") and ai.get("currency"):
        merged["currency"] = ai.get("currency")
    if not merged.get("tax_code") and ai.get("tax_code"):
        merged["tax_code"] = ai.get("tax_code")
    if merged.get("description") in (None, "", "Invoice extraction", "Invoice goods or services") and ai.get("description"):
        merged["description"] = ai.get("description")

    # -- Extended framework fields (not in rule-based base) --------------------
    for field in (
        "supplier_address", "supplier_vat", "supplier_email", "supplier_phone",
        "customer_name", "customer_address", "customer_vat",
        "document_type", "extraction_status", "totals_reconcile",
        "ai_issues", "ai_confidence",
    ):
        if ai.get(field) is not None:
            merged[field] = ai[field]

    return merged


def process_pdf_page(
    pdf_path: str | Path,
    page_index: int,
    openai_api_key: str | None = None,
    account_company_name: str | None = None,
) -> dict[str, Any]:
    pdf_path = Path(pdf_path)

    # Stage 1 — Fast native text extraction (no API cost, always runs).
    native_text = extract_native_pdf_page(pdf_path, page_index)
    method = "native_text"

    # When OpenAI vision is available we send the rendered image directly to
    # the AI, which reads the text from the image itself — so we do NOT need
    # OCR.  OCR is only needed as a last resort when both vision and native
    # text fail to produce enough content.
    use_vision = bool(settings.use_openai and openai_api_key)

    final_text = native_text  # used for rule-based pass and fallback

    if not use_vision:
        # No vision — run OCR if native text is thin
        ocr_backend = get_ocr_backend()
        if count_meaningful_chars(native_text) < 80 and ocr_backend is not None:
            try:
                ocr_text = clean_text(
                    ocr_backend.extract_text_from_pdf_page(pdf_path, page_index, scale=1.8)
                )
                if count_meaningful_chars(ocr_text) > count_meaningful_chars(native_text):
                    final_text = ocr_text
                    method = f"ocr_{ocr_backend.name}"
            except Exception as e:
                logger.warning("OCR failed for page %d: %s", page_index, e)

    if count_meaningful_chars(final_text) == 0:
        final_text = "(page text unavailable)"
        method = f"{method}_empty"

    # Stage 2 — Rule-based extraction from whatever text we have.
    extracted = simple_extract(
        final_text,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
    )

    if use_vision:
        # Stage 3a — Render the page to a JPEG and send directly to the vision
        # model.  The AI reads text straight from the image — far superior to
        # OCR output on scanned / rotated / two-column documents.
        jpeg_b64 = render_page_for_vision(pdf_path, page_index)

        if jpeg_b64:
            ai_fields = openai_extract_invoice_vision(
                jpeg_b64,
                native_text,          # native text as lightweight supplement only
                openai_api_key,
                model=settings.openai_model,
                account_company_name=account_company_name,
            )
            if ai_fields:
                extracted = merge_ai_fields(extracted, ai_fields)
                method = f"{method}+vision"
            else:
                # Vision API call failed — fall back to text-only AI
                logger.info("Vision extraction failed p%d — text-only fallback", page_index)
                _text_for_ai = final_text if count_meaningful_chars(final_text) >= 20 else native_text
                ai_fields = openai_extract_invoice_fields(
                    _text_for_ai, openai_api_key,
                    model=settings.openai_model,
                    account_company_name=account_company_name,
                )
                extracted = merge_ai_fields(extracted, ai_fields)
                method = f"{method}+openai_text"
        else:
            # Image render failed — run OCR then text-only AI as fallback
            ocr_backend = get_ocr_backend()
            if count_meaningful_chars(native_text) < 80 and ocr_backend is not None:
                try:
                    ocr_text = clean_text(
                        ocr_backend.extract_text_from_pdf_page(pdf_path, page_index, scale=1.8)
                    )
                    if count_meaningful_chars(ocr_text) > count_meaningful_chars(native_text):
                        final_text = ocr_text
                        method = f"ocr_{ocr_backend.name}"
                except Exception as e:
                    logger.warning("OCR fallback failed p%d: %s", page_index, e)
            if count_meaningful_chars(final_text) >= 20:
                ai_fields = openai_extract_invoice_fields(
                    final_text, openai_api_key,
                    model=settings.openai_model,
                    account_company_name=account_company_name,
                )
                extracted = merge_ai_fields(extracted, ai_fields)
                method = f"{method}+openai_text"

        # Stage 4 — Second-pass validation
        validation_result = openai_validate_extraction(
            native_text or final_text,
            extracted,
            openai_api_key,
            model=settings.openai_model,
        )
        if validation_result:
            extracted["_validation_result"] = validation_result
            method = f"{method}+validated"

    # -------------------------------------------------------------------------
    # Stage 5 — confidence scoring & business rule checks
    # -------------------------------------------------------------------------
    # Per-section scores from the AI response (0.0 if not available)
    ai_conf = extracted.get("ai_confidence") or {}
    supplier_conf = float(ai_conf.get("supplier", 0.0))
    totals_conf = float(ai_conf.get("totals", 0.0))

    # Rule-based component scores (always computed regardless of AI)
    rule_score = 0.0
    if extracted.get("supplier_name"):
        rule_score += 0.20
    if not suspicious_invoice_number(extracted.get("invoice_number")):
        rule_score += 0.20
    if extracted.get("invoice_date"):
        rule_score += 0.20
    if extracted.get("total_amount") is not None:
        rule_score += 0.20
    if extracted.get("net_amount") is not None:
        rule_score += 0.10
    if extracted.get("vat_amount") is not None:
        rule_score += 0.10
    # Arithmetic reconciliation bonus
    if (
        extracted.get("net_amount") is not None
        and extracted.get("vat_amount") is not None
        and extracted.get("total_amount") is not None
        and round((extracted["net_amount"] + extracted["vat_amount"]) - extracted["total_amount"], 2) == 0
    ):
        rule_score = min(rule_score + 0.05, 1.0)

    # Blend: when AI section scores are available, weight them alongside rules
    if ai_conf:
        ai_overall = (supplier_conf * 0.35 + totals_conf * 0.35
                      + float(ai_conf.get("lines", 0.0)) * 0.15
                      + float(ai_conf.get("customer", 0.0)) * 0.15)
        confidence = round(min(rule_score * 0.50 + ai_overall * 0.50, 0.99), 2)
    else:
        confidence = round(min(rule_score, 0.99), 2)

    # Validation pass can downgrade or confirm status
    validation_result = extracted.pop("_validation_result", None)
    val_status = validation_result.get("validated_status", "passed") if validation_result else None
    val_issues = (validation_result.get("issues") or []) if validation_result else []

    # Map validated_status → our internal status values
    if val_status == "failed":
        final_status = "review"
        review_required = True
    elif val_status == "passed_with_warnings":
        final_status = "review" if confidence < 0.80 else "ok_with_warnings"
        review_required = confidence < 0.80
    else:
        # Threshold upgraded to 0.80 per framework guidance (< 0.80 → review_required)
        final_status = "ok" if confidence >= 0.80 else "review"
        review_required = confidence < 0.80

    # Merge any validation issues into ai_issues
    all_issues = list(extracted.get("ai_issues") or []) + val_issues
    if all_issues:
        extracted["ai_issues"] = all_issues

    # extraction_status from AI or derived
    if not extracted.get("extraction_status"):
        extracted["extraction_status"] = (
            "complete" if confidence >= 0.80
            else "partial" if confidence >= 0.50
            else "review_required"
        )

    # Ensure supplier name is never returned in all-lowercase
    extracted["supplier_name"] = normalise_company_name(extracted.get("supplier_name"))

    extracted.update({
        "page_no": page_index + 1,
        "method_used": method,
        "confidence_score": confidence,
        "validation_status": final_status,
        "review_required": review_required,
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

    raw = _call_openai(prompt, api_key, model, max_tokens=600)
    if not raw:
        return None

    try:
        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0) if m else raw)
        if isinstance(items, list) and items:
            return items
    except Exception as exc:
        logger.warning("openai_extract_line_items JSON parse failed: %s", exc)

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
    account_company_name: str | None = None,
) -> list[dict[str, Any]]:
    page_result = process_pdf_page(
        pdf_path,
        page_index=page_index,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
    )

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
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

    # Length check: require at least 2 letters regardless of total length.
    # This allows short but legitimate names like "M&Z", "A&P", "BP", "G4S"
    # while still blocking single-char or digit-only strings.
    letters = len(re.findall(r"[A-Za-z]", v))
    if letters < 2:
        return True

    digits = len(re.findall(r"\d", v))
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
        # Single standalone common words (qualifiers, articles, taglines) must
        # NOT be combined with the following line — they are not partial company
        # name fragments.  E.g. "Only" + "Quality Foods" → wrong.
        _common_single_words = {
            "only", "the", "a", "an", "our", "your", "my", "its", "their",
            "new", "old", "best", "fresh", "pure", "just", "top", "pro",
            "by", "at", "in", "on", "for", "and", "or", "of", "with",
        }
        first_is_single_qualifier = (
            len(line.split()) == 1
            and line.strip().lower() in _common_single_words
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
            and not first_is_single_qualifier
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


def _clean_ocr_supplier_name(name: str | None) -> str | None:
    """Strip common OCR artefacts from a raw supplier name.

    Handles cases that appear in scanned multi-column invoices:
      "5\\nJ.Sultana\\nBeverages, Wines & Spirits" → "J. Sultana Beverages, Wines & Spirits"
      "jbl\\nJoseph Borg Ltd."                     → "Joseph Borg Ltd."
      "฿ Br Supply Co."                            → "Br Supply Co."
      "N\\nN Calleja Trading"                       → "N Calleja Trading"
    """
    if not name:
        return name
    # Replace embedded newlines/carriage returns with a space
    name = name.replace("\n", " ").replace("\r", " ")
    # Strip leading non-uppercase junk (digits, symbols, short lowercase OCR words)
    # before the first uppercase letter in the string.
    # e.g. "5 J.Sultana" → "J.Sultana",  "jbl Joseph" → "Joseph", "฿ Br" → "Br"
    name = re.sub(r"^[^A-Z]+(?=[A-Z])", "", name)
    # If the name starts with "X Y..." where X is a single uppercase char and Y
    # begins with the same letter (OCR duplicated initial), strip the lone prefix char.
    # e.g. "N N Calleja Trading" → "N Calleja Trading"
    m = re.match(r"^([A-Z])\s+([A-Z]\S.*)$", name)
    if m and m.group(2).upper().startswith(m.group(1)):
        name = m.group(2)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name if len(name) >= 2 else None


def normalise_company_name(name: str | None) -> str | None:
    """Normalise casing of a company name for consistent display.

    Logic (in priority order):
    1. All-lowercase → apply smart title-casing.
    2. ALL-CAPS (every alpha char uppercase, ignoring digits/punctuation) →
       apply smart title-casing (avoids shouting names like "NAAR B.V.").
    3. Mixed case → trust the source (preserves camelCase brands, etc.).

    Short connector words (and, of, for …) are kept lowercase mid-name.
    Common legal suffixes (Ltd, Plc, B.V., GmbH …) keep standard casing.
    """
    if not name:
        return name
    name = name.strip()
    if not name:
        return name

    _lower_words = {"and", "or", "of", "for", "the", "a", "an", "in", "on",
                    "at", "by", "to", "with", "from", "&"}
    # Legal suffix casing overrides (applied after title-casing)
    _suffix_map = {
        "ltd": "Ltd", "limited": "Limited", "plc": "PLC",
        "llp": "LLP", "llc": "LLC", "inc": "Inc",
        "bv": "B.V.", "b.v.": "B.V.", "nv": "N.V.", "n.v.": "N.V.",
        "gmbh": "GmbH", "ag": "AG", "sa": "SA", "sas": "SAS",
        "sl": "SL", "spa": "SpA", "oy": "Oy", "ab": "AB",
        "pty": "Pty", "pty.": "Pty.",
    }

    alpha_chars = [c for c in name if c.isalpha()]
    all_upper = alpha_chars and all(c.isupper() for c in alpha_chars)
    all_lower = alpha_chars and all(c.islower() for c in alpha_chars)

    if not (all_upper or all_lower):
        # Mixed case — trust the source
        return name

    # Apply smart title-casing
    words = name.split()
    result = []
    for i, w in enumerate(words):
        w_low = w.lower().rstrip(".")
        if w_low in _suffix_map:
            result.append(_suffix_map[w_low])
        elif i == 0 or w_low not in _lower_words:
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
        # Specific multi-word labels first (more precise)
        rf"(?:amount due|balance due|grand total|total due|total amount|invoice total|total incl\.?\s*(?:vat|tax)?)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # Generic "total" label
        rf"(?:total)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # Standalone "amount" as a last resort (common on subscription/SaaS invoices)
        rf"(?<!\w)amount\s*[:\-]\s*{_curr}\s*([0-9.,]+)",
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
        "- Normalize dates to YYYY-MM-DD. IMPORTANT: These are European/Maltese invoices.\n"
        "  Dates are printed as dd/mm/yyyy (day first). For example, 05/02/2026 means\n"
        "  5 February 2026, NOT 2 May 2026. Always interpret ambiguous dates as dd/mm/yyyy.\n"
        "- Normalize amounts as plain decimal numbers (no symbols or commas).\n"
        "- NEVER confuse supplier and customer.\n"
        "- Never invent line items or amounts.\n\n"
        "SUPPLIER vs CUSTOMER:\n"
        "- Supplier (issuer/seller): name in the TOP SECTION / LETTERHEAD of the document.\n"
        "  * Usually large bold text, accompanied by address, phone, email, VAT number.\n"
        "  * IMPORTANT: The supplier name is often displayed as a LOGO — a graphical image\n"
        "    with stylised text or initials (e.g. 'M&Z', 'BP', 'A&P Foods'). READ THE LOGO.\n"
        "    If the letterhead is a logo/image, extract the text visible inside it as the\n"
        "    supplier name. Short abbreviated names (2-4 chars) are valid — do not skip them.\n"
        "  * NEVER follows buyer labels: 'Bill To', 'Invoice To', 'To:', 'Customer:',\n"
        "    'Client:', 'Attention:', 'Account Name:', 'Account Ref:', 'Sold To', 'Ship To'.\n"
        "  * Extract ONLY the company trading name as printed. Do NOT prepend standalone\n"
        "    qualifier/tagline words (e.g. 'Only', 'The', 'Our', 'Best', 'Fresh') that\n"
        "    appear near the letterhead but are NOT part of the registered company name.\n"
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


# Circuit-breaker: set to a non-empty string (the error message) once Azure DI
# hits a permanent failure (403 VNet, 401 bad key, etc.) so subsequent pages in
# the same batch don't retry and waste time.
_azure_di_error: str | None = None


def azure_di_available() -> tuple[bool, str | None]:
    """Return (True, None) if Azure DI is configured and has not hit a permanent error.
    Return (False, reason) otherwise.
    """
    global _azure_di_error
    if _azure_di_error:
        return False, _azure_di_error
    if not (settings.use_azure_di and settings.azure_di_endpoint and settings.azure_di_key):
        return False, "Azure DI not configured (USE_AZURE_DI or credentials missing)"
    return True, None


def azure_di_extract_invoice(
    jpeg_bytes: bytes,
    endpoint: str,
    key: str,
) -> dict[str, Any] | None:
    """Extract invoice fields using Azure Document Intelligence prebuilt-invoice model.

    Sends the rendered page JPEG to Azure DI, which uses a purpose-built invoice
    model trained on millions of documents.  Returns the same field schema as the
    OpenAI extraction functions so it slots directly into merge_ai_fields.
    """
    global _azure_di_error

    if not jpeg_bytes or not endpoint or not key:
        return None

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
    except ImportError:
        logger.error("azure-ai-documentintelligence not installed — cannot use Azure DI")
        return None

    def _str(field) -> tuple[str | None, float]:
        if field is None:
            return None, 0.0
        try:
            val = field.value_string or field.content
        except AttributeError:
            val = field.get("valueString") or field.get("content") if isinstance(field, dict) else None
        conf = getattr(field, "confidence", None) or (field.get("confidence", 0.0) if isinstance(field, dict) else 0.0)
        return (val.strip() if val else None), float(conf or 0.0)

    def _num(field) -> tuple[float | None, float]:
        if field is None:
            return None, 0.0
        try:
            raw = field.value_number
            if raw is None:
                raw = field.value_currency.amount if field.value_currency else None
        except AttributeError:
            raw = field.get("valueNumber") if isinstance(field, dict) else None
        conf = getattr(field, "confidence", None) or (field.get("confidence", 0.0) if isinstance(field, dict) else 0.0)
        if raw is None:
            raw = parse_amount(getattr(field, "content", "") or "")
        return (float(raw) if raw is not None else None), float(conf or 0.0)

    def _date(field) -> tuple[str | None, float]:
        if field is None:
            return None, 0.0
        # Always get the raw content text first — it preserves the original
        # date string as printed on the invoice (e.g. "05/02/2026").
        raw_str = getattr(field, "content", None) or (field.get("content") if isinstance(field, dict) else None)
        try:
            val = field.value_date
        except AttributeError:
            val = field.get("valueDate") if isinstance(field, dict) else None
        conf = getattr(field, "confidence", None) or (field.get("confidence", 0.0) if isinstance(field, dict) else 0.0)
        # Locale-aware fix: Azure DI may interpret dd/mm/yyyy dates as mm/dd/yyyy
        # (US format). Re-parsing the raw content string with parse_date() fixes
        # this because parse_date() tries %d/%m/%Y first (European/Maltese locale).
        # Example: Azure DI returns 2026-05-02 for "05/02/2026", but
        # parse_date("05/02/2026") correctly returns 2026-02-05 (February 5).
        if raw_str:
            content_date = parse_date(raw_str.strip())
            if content_date is not None:
                return content_date, float(conf or 0.0)
        # Fallback: use Azure DI's parsed value_date only if content re-parse failed
        if val is not None:
            return parse_date(str(val)), float(conf or 0.0)
        return None, float(conf or 0.0)

    def _addr(field) -> str | None:
        if field is None:
            return None
        try:
            addr_obj = field.value_address
            if addr_obj:
                parts = [
                    getattr(addr_obj, "road", None),
                    getattr(addr_obj, "city", None),
                    getattr(addr_obj, "state", None),
                    getattr(addr_obj, "postal_code", None),
                    getattr(addr_obj, "country_region", None),
                ]
                return ", ".join(p for p in parts if p) or field.content
        except AttributeError:
            pass
        return getattr(field, "content", None) or (field.get("content") if isinstance(field, dict) else None)

    try:
        client = DocumentIntelligenceClient(
            endpoint=endpoint.rstrip("/"),
            credential=AzureKeyCredential(key),
        )
        poller = client.begin_analyze_document(
            "prebuilt-invoice",
            body=jpeg_bytes,
            content_type="image/jpeg",
        )
        result = poller.result()
    except Exception as exc:
        exc_str = str(exc)
        # Permanent failures: open the circuit breaker so we don't retry on every page.
        is_permanent = any(code in exc_str for code in ("403", "401", "VirtualNetwork", "Unauthorized", "Forbidden"))
        if is_permanent:
            _azure_di_error = exc_str[:200]
            logger.error(
                "Azure DI PERMANENTLY unavailable — circuit breaker opened. "
                "Cause: %s. "
                "All pages will fall back to OpenAI vision. "
                "Fix: remove the VNet restriction on the Azure resource or allow this server's IP.",
                exc_str[:300],
            )
        else:
            logger.warning("Azure DI API call failed (transient): %s", exc)
        return None

    if not result.documents:
        logger.info("Azure DI returned no documents")
        return None

    fields = result.documents[0].fields or {}

    # ── Core fields ────────────────────────────────────────────────────────
    supplier_name, s_conf     = _str(fields.get("VendorName"))
    supplier_addr, _          = _str(fields.get("VendorAddress"))
    if not supplier_addr:
        supplier_addr = _addr(fields.get("VendorAddress"))
    supplier_vat, _           = _str(fields.get("VendorTaxId"))

    customer_name, c_conf     = _str(fields.get("CustomerName"))
    customer_addr, _          = _str(fields.get("CustomerAddress"))
    if not customer_addr:
        customer_addr = _addr(fields.get("CustomerAddress"))
    customer_vat, _           = _str(fields.get("CustomerTaxId"))

    invoice_number, _         = _str(fields.get("InvoiceId"))
    invoice_date, _           = _date(fields.get("InvoiceDate"))
    due_date, _               = _date(fields.get("DueDate"))

    net_amount, t_conf_sub    = _num(fields.get("SubTotal"))
    vat_amount, t_conf_tax    = _num(fields.get("TotalTax"))
    total_amount, t_conf_tot  = _num(fields.get("InvoiceTotal"))
    if total_amount is None:
        total_amount, _       = _num(fields.get("AmountDue"))

    currency, _               = _str(fields.get("CurrencyCode"))

    # ── Line items ──────────────────────────────────────────────────────────
    items_field = fields.get("Items")
    line_items: list[dict] = []
    items_conf = 0.0
    if items_field is not None:
        try:
            raw_items = items_field.value_array or []
        except AttributeError:
            raw_items = items_field.get("valueArray", []) if isinstance(items_field, dict) else []
        for item in raw_items:
            try:
                sub = item.value_object or {}
            except AttributeError:
                sub = item.get("valueObject", {}) if isinstance(item, dict) else {}
            desc, _ = _str(sub.get("Description"))
            qty, _  = _num(sub.get("Quantity"))
            uprice, _ = _num(sub.get("UnitPrice"))
            amount, _ = _num(sub.get("Amount"))
            tax, _   = _num(sub.get("Tax"))
            if desc or amount:
                line_items.append({
                    "description": desc,
                    "quantity":    qty,
                    "unit_price":  uprice,
                    "net_amount":  amount,
                    "tax_amount":  tax,   # line-level VAT from Azure DI (may be None)
                })
        if line_items:
            items_conf = min(0.95, 0.70 + 0.05 * len(line_items))

    # ── Description: derive from line items ─────────────────────────────────
    descs = [it["description"] for it in line_items if it.get("description")]
    description = limit_to_20_words("; ".join(descs)) if descs else None

    # ── Per-section confidence scores ───────────────────────────────────────
    totals_conf = round(
        (t_conf_sub + t_conf_tax + t_conf_tot) / max(
            sum(1 for v in (t_conf_sub, t_conf_tax, t_conf_tot) if v > 0), 1
        ), 2
    )

    logger.info(
        "Azure DI extracted: supplier=%r inv=%r total=%s conf=s%.2f/c%.2f/t%.2f",
        supplier_name, invoice_number, total_amount, s_conf, c_conf, totals_conf,
    )

    return {
        "extraction_source": "azure_di",
        "supplier_name":    supplier_name,
        "supplier_address": supplier_addr,
        "supplier_vat":     supplier_vat,
        "customer_name":    customer_name,
        "customer_address": customer_addr,
        "customer_vat":     customer_vat,
        "invoice_number":   invoice_number,
        "invoice_date":     invoice_date,
        "due_date":         due_date,
        "description":      description,
        "net_amount":       net_amount,
        "vat_amount":       vat_amount,
        "total_amount":     total_amount,
        "currency":         currency,
        "tax_code":         None,
        "line_items_structured": line_items,
        "document_type":    "invoice",
        "extraction_status": "complete" if (supplier_name and invoice_number and total_amount) else "partial",
        "ai_confidence": {
            "supplier": round(s_conf, 2),
            "customer": round(c_conf, 2),
            "lines":    round(items_conf, 2),
            "totals":   round(totals_conf, 2),
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
        "- Normalize dates to YYYY-MM-DD. IMPORTANT: These are European/Maltese invoices.\n"
        "  Dates are printed as dd/mm/yyyy (day first). For example, 05/02/2026 means\n"
        "  5 February 2026, NOT 2 May 2026. Always interpret ambiguous dates as dd/mm/yyyy.\n"
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
        "  * Extract ONLY the company trading name as printed. Do NOT prepend standalone\n"
        "    qualifier/tagline words (e.g. 'Only', 'The', 'Our', 'Best', 'Fresh') that\n"
        "    appear near the letterhead but are NOT part of the registered company name.\n"
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


def merge_ai_fields(
    base: dict[str, Any],
    ai: dict[str, Any] | None,
    account_company_name: str | None = None,
) -> dict[str, Any]:
    """Merge AI-extracted fields into the rule-based base result.

    Core fields follow a "trust AI unless obviously wrong" strategy.
    Extended framework fields (supplier_address, customer_name, etc.) are
    copied across directly — they don't exist in the rule-based result.

    account_company_name: the buyer's own company name.  Any AI-returned
    supplier that matches this name is silently blocked — the same guard
    the rule-based extractor already applies via account_tokens.
    """
    if not ai:
        return base

    merged = dict(base)

    # -- Supplier name ---------------------------------------------------------
    # Strategy: trust the rule-based result when it found something plausible,
    # because it anchors strictly to position-0 / letterhead text.
    # Only override with the AI result when:
    #   (a) the rule-based result is absent or suspicious, OR
    #   (b) AI has sufficient confidence to confirm a better name.
    #
    # Threshold differs by source:
    #   • Azure DI uses a dedicated VendorName field, so a moderate gate (≥ 0.6)
    #     is used; the account-company hard-block above handles the main risk.
    #   • OpenAI vision/text reads free-form — keep the stricter ≥ 0.85 gate to
    #     prevent it from picking up a customer name printed elsewhere on the page.
    ai_supplier = ai.get("supplier_name")

    # Hard-block: if AI returned the account holder's own name as supplier,
    # discard it — same logic as the rule-based account_tokens guard.
    # This catches cases where Azure DI or OpenAI reads a prominent "Bill To"
    # customer block and mistakenly treats it as the vendor.
    # IMPORTANT: use whole-word matching (re.search with \b) — plain substring
    # matching causes false positives, e.g. "FOOD" matching inside "FOODS".
    _acct_block_tokens: frozenset[str] = frozenset()
    if account_company_name:
        _acct_block_tokens = _build_account_tokens(account_company_name)

    if ai_supplier and _acct_block_tokens and any(
        re.search(r"\b" + re.escape(tok) + r"\b", ai_supplier, re.I)
        for tok in _acct_block_tokens
    ):
        logger.info(
            "merge_ai_fields: AI supplier '%s' matches account company '%s' — blocked",
            ai_supplier, account_company_name,
        )
        ai_supplier = None

    # Also apply the same hard-block to the rule-based result sitting in merged.
    # find_supplier_name uses account_tokens internally but can still slip through
    # on logo-only pages where the buyer name is the only readable text.
    if merged.get("supplier_name") and _acct_block_tokens and any(
        re.search(r"\b" + re.escape(tok) + r"\b", merged["supplier_name"], re.I)
        for tok in _acct_block_tokens
    ):
        logger.info(
            "merge_ai_fields: rule-based supplier '%s' matches account company '%s' — blocked",
            merged["supplier_name"], account_company_name,
        )
        merged["supplier_name"] = None

    ai_supplier_conf = float((ai.get("ai_confidence") or {}).get("supplier", 0.0))
    is_azure_di = ai.get("extraction_source") == "azure_di"
    conf_threshold = 0.6 if is_azure_di else 0.85
    rule_supplier_ok = bool(
        merged.get("supplier_name")
        and not suspicious_supplier_name(merged.get("supplier_name"))
    )
    if not rule_supplier_ok:
        # Rule-based found nothing useful — take AI regardless of confidence
        if ai_supplier and not suspicious_supplier_name(ai_supplier):
            merged["supplier_name"] = ai_supplier
    else:
        # Rule-based found something — upgrade to AI result if AI is sufficiently confident
        if ai_supplier and not suspicious_supplier_name(ai_supplier) and ai_supplier_conf >= conf_threshold:
            merged["supplier_name"] = ai_supplier
    # Clean OCR artefacts (embedded newlines, leading junk chars) then normalise casing.
    merged["supplier_name"] = normalise_company_name(
        _clean_ocr_supplier_name(merged.get("supplier_name"))
    )

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
        "line_items_structured",   # structured list from Azure DI / OpenAI
        "extraction_source",       # tracks which engine produced the result
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

    _di_ok, _di_reason = azure_di_available()
    use_azure_di = _di_ok
    if not _di_ok and settings.use_azure_di:
        logger.debug("Azure DI skipped: %s", _di_reason)

    if use_azure_di or use_vision:
        # ── Render the page to raw JPEG bytes (shared by Azure DI and OpenAI vision)
        try:
            jpeg_bytes = OCRBackend.render_pdf_page_to_jpeg_bytes(
                pdf_path, page_index, scale=1.5, quality=80
            )
            if jpeg_bytes and len(jpeg_bytes) > 4 * 1024 * 1024:
                jpeg_bytes = OCRBackend.render_pdf_page_to_jpeg_bytes(
                    pdf_path, page_index, scale=1.0, quality=60
                )
        except Exception as exc:
            logger.warning("JPEG render failed p%d: %s", page_index, exc)
            jpeg_bytes = None

        ai_fields = None

        # ── Stage 3a: Azure Document Intelligence (primary, highest accuracy) ──
        if use_azure_di and jpeg_bytes:
            ai_fields = azure_di_extract_invoice(
                jpeg_bytes,
                settings.azure_di_endpoint,
                settings.azure_di_key,
            )
            if ai_fields:
                extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
                method = f"{method}+azure_di"
                logger.info("Azure DI extraction succeeded for page %d", page_index)

        # ── Stage 3b: OpenAI vision (fallback if Azure DI not available/failed) ─
        if ai_fields is None and use_vision and jpeg_bytes:
            jpeg_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
            ai_fields = openai_extract_invoice_vision(
                jpeg_b64,
                native_text,
                openai_api_key,
                model=settings.openai_model,
                account_company_name=account_company_name,
            )
            if ai_fields:
                extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
                method = f"{method}+vision"

        # ── Stage 3c: Text-only AI (fallback when image unavailable) ────────────
        if ai_fields is None and use_vision:
            logger.info("Image unavailable p%d — text-only AI fallback", page_index)
            _text_for_ai = final_text if count_meaningful_chars(final_text) >= 20 else native_text
            if count_meaningful_chars(_text_for_ai) >= 20:
                ai_fields = openai_extract_invoice_fields(
                    _text_for_ai, openai_api_key,
                    model=settings.openai_model,
                    account_company_name=account_company_name,
                )
                if ai_fields:
                    extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
                    method = f"{method}+openai_text"

        # ── Stage 4: OpenAI validation pass (runs after Azure DI too if available)
        if use_vision and openai_api_key:
            validation_result = openai_validate_extraction(
                native_text or final_text,
                extracted,
                openai_api_key,
                model=settings.openai_model,
            )
            if validation_result:
                extracted["_validation_result"] = validation_result
                method = f"{method}+validated"

    # ── Net → Total fallback ─────────────────────────────────────────────────
    # On subscription / zero-VAT invoices (reverse charge, VAT-exempt, SaaS)
    # the invoice often has only one amount with no explicit "Total" label.
    # If we have a net_amount but total_amount is still null AND vat is absent
    # or zero, treat net as the total — it's the only amount on the document.
    if (
        extracted.get("total_amount") is None
        and extracted.get("net_amount") is not None
        and not extracted.get("vat_amount")
    ):
        extracted["total_amount"] = extracted["net_amount"]
        logger.debug("Net→Total fallback applied: total set to %.2f", extracted["net_amount"])

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

    # ── Smart review flagging ─────────────────────────────────────────────────
    # Review is only required for CRITICAL issues that prevent posting:
    #   • Missing supplier name  (can't post without knowing who issued it)
    #   • Missing total amount   (can't process payment without a value)
    #   • Hard AI validation failure
    # Medium/low confidence on a COMPLETE extraction is not a reason to block —
    # it generates a warning status instead so accountants can spot-check
    # without being forced to open every invoice.
    missing_supplier = not extracted.get("supplier_name")
    missing_amount   = extracted.get("total_amount") is None

    if val_status == "failed":
        final_status   = "review_validation_failed"
        review_required = True
    elif missing_supplier and missing_amount:
        final_status   = "review_incomplete"
        review_required = True
    elif missing_supplier:
        final_status   = "review_no_supplier"
        review_required = True
    elif missing_amount:
        final_status   = "review_no_amount"
        review_required = True
    elif val_status == "passed_with_warnings":
        final_status   = "ok_warned"
        review_required = False
    elif confidence < 0.65:
        final_status   = "ok_warned"
        review_required = False
    else:
        final_status   = "ok"
        review_required = False

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

    # Ensure supplier name has OCR artefacts removed and casing normalised.
    extracted["supplier_name"] = normalise_company_name(
        _clean_ocr_supplier_name(extracted.get("supplier_name"))
    )

    # ── Build header/totals evidence strings ─────────────────────────────────
    # When the page has no usable text layer (scanned image), final_text is
    # "(page text unavailable)".  Synthesize readable evidence from what the
    # AI actually extracted so that reviewers can see what was found.
    _text_is_unavailable = (
        not final_text
        or count_meaningful_chars(final_text) < 20
        or final_text.startswith("(page text unavailable")
    )

    if _text_is_unavailable:
        # Synthesize header from extracted fields
        _header_parts: list[str] = []
        if extracted.get("supplier_name"):
            _header_parts.append(f"Supplier: {extracted['supplier_name']}")
        if extracted.get("supplier_vat"):
            _header_parts.append(f"VAT No: {extracted['supplier_vat']}")
        if extracted.get("invoice_number"):
            _header_parts.append(f"Invoice No: {extracted['invoice_number']}")
        if extracted.get("invoice_date"):
            _header_parts.append(f"Date: {extracted['invoice_date']}")
        if extracted.get("customer_name"):
            _header_parts.append(f"Customer: {extracted['customer_name']}")
        header_raw = " | ".join(_header_parts) if _header_parts else f"[Scanned — extracted via {method}]"

        # Synthesize totals from extracted amounts
        _totals_parts: list[str] = []
        if extracted.get("net_amount") is not None:
            _totals_parts.append(f"Net: {extracted['net_amount']:.2f}")
        if extracted.get("vat_amount") is not None:
            _totals_parts.append(f"VAT: {extracted['vat_amount']:.2f}")
        if extracted.get("total_amount") is not None:
            _totals_parts.append(f"Total: {extracted['total_amount']:.2f}")
        if extracted.get("currency"):
            _totals_parts.append(f"Currency: {extracted['currency']}")
        totals_raw = " | ".join(_totals_parts) if _totals_parts else None

        page_text_raw = f"[Scanned page — no text layer — extracted via {method}]\n" + header_raw
    else:
        header_raw = "\n".join(final_text.splitlines()[:12])
        totals_raw = "\n".join(final_text.splitlines()[-10:])
        page_text_raw = final_text[:20000]

    extracted.update({
        "page_no": page_index + 1,
        "method_used": method,
        "confidence_score": confidence,
        "validation_status": final_status,
        "review_required": review_required,
        "header_raw": header_raw,
        "totals_raw": totals_raw,
        "page_text_raw": page_text_raw,
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
            row["validation_status"] = "review_amount_mismatch"
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

        # Build a readable line_items_raw from the structured item.
        # Accept both "amount" (openai_extract_line_items) and "net_amount"
        # (Azure DI / OpenAI vision line_items_structured).
        raw_amt = item.get("amount") if item.get("amount") is not None else item.get("net_amount")
        parts = [row["description"]]
        if item.get("quantity") is not None:
            parts.append(f"Qty: {item['quantity']}")
        if item.get("unit_price") is not None:
            parts.append(f"@ {item['unit_price']}")
        if raw_amt is not None:
            parts.append(f"= {raw_amt}")
        row["line_items_raw"] = "  ".join(parts)

        amount = None
        if raw_amt is not None:
            try:
                amount = float(raw_amt)
            except (TypeError, ValueError):
                amount = parse_amount(str(raw_amt))

        if amount is not None:
            summed += amount
            # Use Azure DI's per-line tax if present; default to 0.0
            line_vat = item.get("tax_amount")
            if line_vat is not None:
                try:
                    line_vat = float(line_vat)
                except (TypeError, ValueError):
                    line_vat = 0.0
            else:
                line_vat = 0.0
            row["net_amount"] = amount
            row["vat_amount"] = line_vat
            row["total_amount"] = round(amount + line_vat, 2)

        row["line_no"] = idx
        rows.append(row)

    if not rows:
        return [page_result]

    # Cross-check against the invoice-level total
    invoice_total = page_result.get("total_amount")
    if invoice_total is not None and abs(float(invoice_total) - summed) > 0.10:
        for row in rows:
            row["review_required"] = True
            row["validation_status"] = "review_amount_mismatch"

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
        # ── Line-item extraction priority (tallest accuracy first) ─────────
        # 1. Azure Document Intelligence (primary — prebuilt-invoice model,
        #    extracts line items with qty / unit_price / net_amount / tax_amount).
        # 2. OpenAI vision (fallback — reads line items from the page image).
        #    Both #1 and #2 populate line_items_structured during process_pdf_page.
        # 3. OpenAI text-only (second fallback for text-layer PDFs).
        # 4. Rule-based splitter (last resort — parses line_items_raw text).
        structured = page_result.get("line_items_structured")
        if structured and isinstance(structured, list) and len(structured) > 0:
            engine = page_result.get("method_used", "")
            src = "azure_di" if "azure_di" in engine else "openai_vision"
            logger.info(
                "Lines mode: using %d structured item(s) from %s", len(structured), src
            )
            return _build_rows_from_ai_items(page_result, structured)

        # Priority 3: OpenAI text-only line item extraction.
        if settings.use_openai and openai_api_key:
            page_text = page_result.get("page_text_raw") or ""
            ai_items = openai_extract_line_items(page_text, openai_api_key, model=settings.openai_model)
            if ai_items:
                logger.info("Lines mode: using %d item(s) from openai_text", len(ai_items))
                return _build_rows_from_ai_items(page_result, ai_items)

        # Priority 4: Rule-based splitter on the raw candidate lines.
        logger.info("Lines mode: falling back to rule-based line splitter")
        return split_line_item_rows(page_result)

    return [page_result]


def process_pdf(pdf_path: str | Path, openai_api_key: str | None = None) -> list[dict[str, Any]]:
    page_count = get_pdf_page_count(pdf_path)
    return [
        process_pdf_page(pdf_path, page_index=i, openai_api_key=openai_api_key)
        for i in range(page_count)
    ]
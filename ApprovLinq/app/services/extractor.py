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

# New pipeline modules — imported lazily inside functions to avoid circular deps
# at module load time; direct imports kept here for type checking.
try:
    from app.services.parse_dates import parse_invoice_date, ParsedDate
    from app.services.normalize_suppliers import normalize_supplier as _normalize_supplier
    from app.services.validate_invoice import validate_invoice as _validate_invoice
    from app.services.review_engine import compute_review_decision
    from app.services.preprocess import preprocess_page as _preprocess_page
    _NEW_MODULES_AVAILABLE = True
except ImportError as _imp_err:
    _NEW_MODULES_AVAILABLE = False
    import logging as _log
    _log.getLogger(__name__).warning("New pipeline modules not available: %s", _imp_err)

logger = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_meaningful_chars(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]", text or ""))


def preprocess_page_image(jpeg_bytes: bytes) -> tuple[bytes, float]:
    """Stage 1 image preprocessing: adaptive 2-level darkening, bleed-through
    suppression, and page quality scoring.

    Darkening levels are chosen based on measured page brightness BEFORE any
    enhancement — so the decision is made on the raw scan with no extra API
    calls:

        Level 0 (quality ≥ 0.68) — Good scan: minimal touch-up only.
            Contrast ×1.25, Sharpness ×1.15. No brightness adjustment.
        Level 1 (0.40 ≤ quality < 0.68) — Medium scan: standard darkening.
            Contrast ×1.55, Sharpness ×1.30, optional gentle brightness lift.
        Level 2 (quality < 0.40) — Faded/light scan: aggressive darkening.
            Contrast ×1.90, Sharpness ×1.50, stronger brightness correction,
            unsharp-mask to recover soft edges.

    Quality score returned is computed on the RAW image (pre-enhancement) so
    it reflects actual scan quality, not the artificially boosted result.

    Returns:
        (processed_jpeg_bytes, quality_score 0.0–1.0)
    """
    try:
        import io
        import statistics
        from PIL import Image, ImageEnhance, ImageFilter

        img = Image.open(io.BytesIO(jpeg_bytes))

        # Ensure RGB for consistent processing
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # ── Quality scoring on the RAW image (before any enhancement) ──────
        gray = img.convert("L")
        pixels = list(gray.getdata())
        mean_px   = sum(pixels) / max(len(pixels), 1)
        try:
            std_px = statistics.stdev(pixels)
        except statistics.StatisticsError:
            std_px = 0.0

        # High std → high contrast → good scan
        # Brightness ideally around 128 (not too dark, not too washed out)
        contrast_score   = min(std_px / 75.0, 1.0)
        brightness_score = 1.0 - abs(mean_px - 128.0) / 128.0
        quality_score    = round(contrast_score * 0.65 + brightness_score * 0.35, 2)

        # ── Bleed-through suppression (always) ─────────────────────────────
        # A small median filter knocks out fine ink-bleed noise from thin paper
        # without affecting character edges.  Applied before enhancement so the
        # noise is removed before we amplify contrast.
        img = img.filter(ImageFilter.MedianFilter(size=3))

        # ── Adaptive enhancement — 2 levels based on raw quality ───────────
        if quality_score >= 0.68:
            # Level 0 — good scan: conservative touch-up
            img = ImageEnhance.Contrast(img).enhance(1.25)
            img = ImageEnhance.Sharpness(img).enhance(1.15)
            level = 0

        elif quality_score >= 0.40:
            # Level 1 — medium scan: standard darkening
            img = ImageEnhance.Contrast(img).enhance(1.55)
            img = ImageEnhance.Sharpness(img).enhance(1.30)
            if mean_px > 180:          # washed-out / very bright page
                img = ImageEnhance.Brightness(img).enhance(0.88)
            elif mean_px < 100:        # unusually dark scan
                img = ImageEnhance.Brightness(img).enhance(1.18)
            level = 1

        else:
            # Level 2 — faded / very light scan: aggressive darkening
            img = ImageEnhance.Contrast(img).enhance(1.90)
            img = ImageEnhance.Sharpness(img).enhance(1.50)
            if mean_px > 160:          # faded page
                img = ImageEnhance.Brightness(img).enhance(0.80)
            elif mean_px < 90:         # very dark scan (inverted / over-exposed)
                img = ImageEnhance.Brightness(img).enhance(1.25)
            # Unsharp mask to recover soft letter edges on very faded originals
            img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=3))
            level = 2

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        processed = buf.getvalue()

        logger.debug(
            "preprocess_page_image: level=%d quality=%.2f (contrast=%.2f "
            "brightness=%.2f mean_px=%.0f) input=%d bytes output=%d bytes",
            level, quality_score, contrast_score, brightness_score, mean_px,
            len(jpeg_bytes), len(processed),
        )
        return processed, quality_score

    except Exception as exc:
        logger.warning("preprocess_page_image failed (using original): %s", exc)
        return jpeg_bytes, 0.5


def _check_deposit_component(
    net: float | None,
    vat: float | None,
    total: float | None,
) -> tuple[bool, float]:
    """Check whether a totals mismatch is plausibly explained by a deposit
    or returnable-container component (e.g. BCRS in Malta).

    Returns:
        (True, deposit_amount) if a deposit is the likely cause.
        (False, 0.0)           otherwise.

    Heuristic: the unexplained difference is positive (additional charge),
    small (≤ €25), and lands on a "round" value (whole euro or 50-cent
    multiples) — consistent with per-unit BCRS / deposit charges.
    """
    if net is None or total is None:
        return False, 0.0
    vat_val = float(vat or 0.0)
    diff = round(float(total) - (float(net) + vat_val), 2)
    if 0.01 <= diff <= 25.00:
        # Round-number check: whole euro, 50c, 25c, or 10c multiples
        if round(diff % 1.0, 2) in (0.0, 0.10, 0.25, 0.50, 0.75):
            return True, diff
    return False, 0.0


def _collect_review_reasons(
    extracted: dict[str, Any],
    validation_result: dict[str, Any] | None,
) -> list[str]:
    """Compute a list of specific reason codes explaining why this invoice
    row may need human review.

    Reason codes (pipe-joined when stored):
        no_supplier             — supplier name could not be extracted
        invoice_number_missing  — invoice number absent or looks like a label
        no_amount               — total amount is missing
        ambiguous_date_locale   — date is ambiguous (day == month, both ≤ 12)
        vat_missing             — total > net but no VAT captured
        vat_anomaly             — VAT rate is implausible (> 35% or < 2%)
        totals_mismatch         — net + vat ≠ total (beyond tolerance)
        deposit_component_detected:<amount> — mismatch explained by deposit
        ai_validation_failed    — OpenAI validation pass returned 'failed'
        ai_validation_warned    — OpenAI validation pass returned warnings
        low_confidence          — overall confidence < 0.55
    """
    reasons: list[str] = []

    if not extracted.get("supplier_name"):
        reasons.append("no_supplier")

    if suspicious_invoice_number(extracted.get("invoice_number")):
        reasons.append("invoice_number_missing")

    if extracted.get("total_amount") is None:
        reasons.append("no_amount")

    # Ambiguous date: both day AND month ≤ 12 (could be read either way)
    d = extracted.get("invoice_date")
    if d and hasattr(d, "day"):
        if d.day <= 12 and d.month <= 12 and d.day != d.month:
            reasons.append("ambiguous_date_locale")

    net = extracted.get("net_amount")
    vat = extracted.get("vat_amount")
    total = extracted.get("total_amount")

    if net is not None and total is not None and vat is None:
        if float(total) > float(net) * 1.02:
            reasons.append("vat_missing")

    if net is not None and vat is not None and float(net) > 0:
        vat_rate = float(vat) / float(net)
        if vat_rate > 0.35 or (0.0 < vat_rate < 0.015):
            reasons.append("vat_anomaly")

    if net is not None and vat is not None and total is not None:
        diff = abs((float(net) + float(vat)) - float(total))
        if diff > 0.10:
            is_deposit, deposit_amt = _check_deposit_component(net, vat, total)
            if is_deposit:
                reasons.append(f"deposit_component_detected:{deposit_amt:.2f}")
            else:
                reasons.append("totals_mismatch")

    if validation_result:
        vs = validation_result.get("validated_status", "passed")
        if vs == "failed":
            reasons.append("ai_validation_failed")
        elif vs == "passed_with_warnings":
            reasons.append("ai_validation_warned")

    conf = extracted.get("_confidence")
    if conf is not None and float(conf) < 0.55:
        reasons.append("low_confidence")

    return reasons


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
      "Br Supply Co. Br Supply Co"                 → "Br Supply Co."  (full-name OCR repeat)
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
    # Detect full-name OCR duplication: "Acme Ltd. Acme Ltd" or "Acme Ltd Acme Ltd"
    # Split on ". " (period-space) or double-space, check if both halves share the same
    # significant token prefix — if so, keep the longer (punctuated) version.
    if len(name) > 8:
        for sep in (". ", "  "):
            parts = name.split(sep, 1)
            if len(parts) == 2:
                first, rest = parts[0].strip(), parts[1].strip()
                first_norm = re.sub(r"[^A-Za-z0-9]", "", first).lower()
                rest_norm  = re.sub(r"[^A-Za-z0-9]", "", rest).lower()
                if (
                    len(first_norm) >= 4
                    and len(rest_norm) >= 4
                    and (
                        first_norm == rest_norm
                        or first_norm.startswith(rest_norm[:max(4, len(rest_norm) // 2)])
                        or rest_norm.startswith(first_norm[:max(4, len(first_norm) // 2)])
                    )
                ):
                    name = first if len(first) >= len(rest) else rest
                    break
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
        # Food/beverage expanded for Malta hospitality and wholesale suppliers
        ("food and beverage supplies", [
            "food", "catering", "beverage", "drink", "snack", "bread", "meat",
            "poultry", "chicken", "beef", "pork", "fish", "seafood", "dairy",
            "cheese", "butter", "milk", "cream", "yoghurt", "eggs",
            "whisky", "whiskey", "vodka", "gin", "rum", "wine", "beer",
            "spirits", "alcohol", "champagne", "prosecco", "brandy", "liqueur",
            "scotch", "bourbon", "lager", "cider",
            "coffee", "tea", "juice", "water", "soft drink", "energy drink",
            "ice cream", "frozen", "chilled", "deli", "produce",
        ]),
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
        # Prefer the first non-numeric, non-trivial line for a readable description
        for line in lines[:3]:
            if re.search(r"^[\d\s.,€£$%]+$", line):
                continue
            clean = re.sub(r"\s{2,}", " ", line).strip()
            if len(clean) > 4:
                return limit_to_20_words(clean)
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




def _extract_structured_summary_totals(text: str) -> dict | None:
    """Parse invoices that have explicit structured summary/analysis blocks.

    Handles layouts with headings like:
      Tax Analysis         → authoritative net + vat
      Invoice Summary      → authoritative total_amount
      Deposit Summary      → deposit candidate for BCRS split
      Gross Value / Total Discount / Less Returns / Sub Total / Invoice Total

    Rules (all generic — no supplier names hardcoded):
    1. If a "Tax Analysis" block is found, use its TOTAL row as net + vat.
    2. If an "Invoice Summary" block is found, use its bottom total as total_amount.
    3. If a "Deposit Summary" / "Deposits" block is found, capture its value as
       a deposit candidate (stored in _deposit_candidate; used by BCRS split).
    4. Reconciliation: prefer a net+vat combination that matches total_amount
       within €0.10 tolerance.
    5. Do NOT let Gross Value, Total Discount or Deposits lines replace
       a reconciled net/vat/total set.

    Returns a dict with any of: net_amount, vat_amount, total_amount,
    _deposit_candidate, or None if no structured block was found.
    """
    import re as _re

    if not text:
        return None

    lines = text.splitlines()

    def _money(s: str) -> float | None:
        s = (s or "").strip()
        # strip currency symbols / separators
        s = _re.sub(r"[€£$,]", "", s).replace(" ", "")
        # European decimal: 1.234,56 → 1234.56
        if _re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", s):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        try:
            v = float(s)
            return round(v, 2)
        except ValueError:
            return None

    def _find_block_end(start: int, next_heading_re) -> int:
        for i in range(start + 1, min(start + 30, len(lines))):
            if next_heading_re.match(lines[i].strip()):
                return i
        return min(start + 20, len(lines))

    # Heading patterns (case-insensitive, generous matching)
    _H_TAX      = _re.compile(r"tax\s+analysis", _re.I)
    _H_INV_SUM  = _re.compile(r"invoice\s+summary", _re.I)
    # Heading lines must NOT be followed by a numeric value on the same line
    # (otherwise a data row like "Deposits   9.60" is wrongly treated as a heading)
    _H_DEP_SUM  = _re.compile(r"(?:deposit\s+summary|deposits?\s*summary)\s*$", _re.I)
    _H_ANY      = _re.compile(r"^(tax\s+analysis|invoice\s+summary|deposit\s+summary)\s*$", _re.I)

    # Pattern: "LABEL   123.45" or "LABEL: 123.45" where number is last token
    _ROW = _re.compile(
        r"^(.+?)\s{2,}([+-]?\d[\d,. ]*\d|\d+\.\d{2})\s*$|"   # 2+ spaces
        r"^(.+?)\s*[:\-]\s*([+-]?\d[\d,. ]*\d|\d+\.\d{2})\s*$|"  # colon/dash separator
        r"^(.+?)\s+([+-]?\d+\.\d{2})\s*$"                         # single space + x.xx
    )

    def _parse_block_rows(start: int, end: int) -> list[tuple[str, float]]:
        """Return (label_lower, value) pairs from a block of lines."""
        pairs = []
        for i in range(start, end):
            ln = lines[i].strip()
            if not ln:
                continue
            m = _ROW.match(ln)
            if not m:
                continue
            label = (m.group(1) or m.group(3) or m.group(5) or "").strip().lower()
            val_str = (m.group(2) or m.group(4) or m.group(6) or "").strip()
            v = _money(val_str)
            if v is not None and label:
                pairs.append((label, v))
        return pairs

    # ── Locate heading lines ──────────────────────────────────────────────────
    tax_idx = inv_idx = dep_idx = -1
    for i, ln in enumerate(lines):
        ls = ln.strip()
        if tax_idx < 0 and _H_TAX.search(ls):
            tax_idx = i
        elif inv_idx < 0 and _H_INV_SUM.search(ls):
            inv_idx = i
        elif dep_idx < 0 and _H_DEP_SUM.search(ls):
            dep_idx = i

    # If no structured headings found, return None so generic extraction handles it
    if tax_idx < 0 and inv_idx < 0 and dep_idx < 0:
        return None

    result: dict = {}

    # ── Tax Analysis block → net + vat ───────────────────────────────────────
    if tax_idx >= 0:
        end = _find_block_end(tax_idx, _H_ANY)
        rows = _parse_block_rows(tax_idx + 1, end)
        # Look for "total" row at the bottom of the tax analysis block
        # It should contain net + vat combined
        ta_total = None
        ta_tax   = None
        ta_net   = None
        for label, val in rows:
            if "total" in label and "vat" not in label and "tax" not in label and val > 0:
                ta_total = val
            if any(k in label for k in ("vat", "tax amount", "tax total", "v.a.t")):
                ta_tax = val
            if any(k in label for k in ("net", "sub total", "subtotal", "gross value",
                                         "nett", "excl")):
                if ta_net is None or val > ta_net:  # take largest plausible net
                    ta_net = val

        # Authoritative: if we found a tax-analysis TOTAL (net+vat combined)
        # and a VAT amount, derive net = total - vat
        if ta_total is not None and ta_tax is not None and ta_total > 0:
            derived_net = round(ta_total - ta_tax, 2)
            if derived_net > 0:
                result["net_amount"] = derived_net
                result["vat_amount"] = ta_tax
        elif ta_net is not None and ta_tax is not None:
            result["net_amount"] = ta_net
            result["vat_amount"] = ta_tax

    # ── Invoice Summary block → total_amount ─────────────────────────────────
    if inv_idx >= 0:
        end = _find_block_end(inv_idx, _H_ANY)
        rows = _parse_block_rows(inv_idx + 1, end)
        # Prefer "invoice total" or "sub total" at the bottom as total_amount
        # Reject "gross value" and "total discount" as they are intermediate lines
        _SKIP = _re.compile(r"gross\s*value|total\s*discount|less\s*return|less\s*disc|deposits?", _re.I)
        candidates = []
        for label, val in rows:
            if _SKIP.search(label):
                continue
            if any(k in label for k in ("invoice total", "sub total", "subtotal",
                                         "total payable", "amount due", "balance due",
                                         "net payable")):
                candidates.append(val)
        if candidates:
            result["total_amount"] = candidates[-1]  # last match is most likely the bottom total

    # ── Deposit Summary block → deposit candidate ─────────────────────────────
    if dep_idx >= 0:
        end = _find_block_end(dep_idx, _H_ANY)
        rows = _parse_block_rows(dep_idx + 1, end)
        # Look for a specific deposit amount (not zero)
        for label, val in rows:
            if val > 0.0 and any(k in label for k in ("deposit", "bcrs", "total", "returnable")):
                result["_deposit_candidate"] = val
                break

    # ── Reconciliation pass ───────────────────────────────────────────────────
    # If we have all three, validate; reject if they don't reconcile
    net   = result.get("net_amount")
    vat   = result.get("vat_amount")
    total = result.get("total_amount")
    dep   = result.get("_deposit_candidate")

    if net and vat and total:
        tol = 0.10
        base_ok = abs((net + vat) - total) <= tol
        dep_ok  = dep and abs((net + vat + dep) - total) <= tol
        if not base_ok and not dep_ok:
            # Reconciliation failed — drop structured result and let generic handle it
            logger.debug(
                "_extract_structured_summary_totals: reconciliation failed "
                "net=%.2f vat=%.2f total=%.2f dep=%s — falling back to generic",
                net, vat, total, dep,
            )
            return None

    if not result:
        return None

    logger.debug("_extract_structured_summary_totals: result=%s", result)
    return result


def _invoice_number_fallback(text: str) -> str | None:
    """Fallback invoice-number extractor for Nectar-style layouts.

    Called only when the standard first_match patterns in simple_extract
    return nothing.  Searches the top header region (first ~40 lines) for
    a label / value pair using a broader set of label keywords and more
    lenient value patterns.

    Priority (highest first):
      1. Same line as label  — e.g. "Invoice No: 45005"
      2. Right of label in same visual block
      3. Nearby line above/below in the same header box

    Hard rejections — candidates that match these are never returned:
      • pure VAT/tax numbers  (MT12345678 style, or "VAT No:" prefix)
      • dates  (nn/nn/nnnn)
      • phone / fax numbers
      • customer / route / sales rep codes
      • purely alphabetic strings
    """
    if not text:
        return None

    # Only examine the top 40 lines — invoice number is always in the header
    lines = text.splitlines()[:40]

    # Labels that indicate an invoice number is nearby
    _LABEL_RE = re.compile(
        r"\b(invoice\s*(?:no\.?|number|#|nr\.?)|doc(?:ument)?\s*(?:no\.?|number|nr\.?)"
        r"|ref(?:erence)?\s*(?:no\.?|number)?|number)\s*[.:\-]?",
        re.I,
    )

    # Hard-reject patterns: these look like invoice numbers but are not
    _REJECT_RE = re.compile(
        r"(?:"
        r"\b(?:vat|tax)\s*(?:no\.?|number|reg\.?|registration)[\s:.]*"       # VAT No
        r"|\b(?:MT|GB|EU)\d{8,}"                                              # VAT reg numbers
        r"|\b\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b"                         # dates
        r"|\b(?:tel|fax|phone|mob)\b"                                         # contact
        r"|\b(?:customer|client|route|sales\s*rep|rep)\b"                     # relationship fields
        r")",
        re.I,
    )

    # Acceptable invoice-number token: has at least one digit, not purely alpha,
    # not a bare year, and not too long to be a real invoice ref
    def _is_valid_candidate(val: str) -> bool:
        v = val.strip()
        if not v or len(v) < 2 or len(v) > 30:
            return False
        if not re.search(r"\d", v):          # must contain at least one digit
            return False
        if re.match(r"^(19|20)\d{2}$", v):  # bare year
            return False
        if re.match(r"^\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}$", v):  # date
            return False
        if _REJECT_RE.search(v):
            return False
        return True

    # Pattern for a plausible invoice-number token (alphanumeric, slashes, dashes)
    _TOKEN_RE = re.compile(r"([A-Z0-9][A-Z0-9\/\-_\.]*[0-9][A-Z0-9\/\-_\.]*|[0-9]{2,})", re.I)

    for idx, line in enumerate(lines):
        lm = _LABEL_RE.search(line)
        if lm is None:
            continue
        # Skip if the line itself carries a hard-reject pattern (e.g. "VAT No:")
        if _REJECT_RE.search(line[:lm.start()]):
            continue

        # Priority 1 & 2: same line, after the label
        after = line[lm.end():].strip()
        for tok in _TOKEN_RE.findall(after):
            if _is_valid_candidate(tok):
                logger.debug("invoice_number_fallback: same-line hit %r on %r", tok, line)
                return tok

        # Priority 3: next 1-2 lines (value on the following line)
        for nidx in range(idx + 1, min(idx + 3, len(lines))):
            nline = lines[nidx].strip()
            if not nline:
                continue
            # Stop if we hit another label line (different header field)
            if re.search(r"\b(date|vat|tax|customer|name|address|total|amount)\b", nline, re.I):
                break
            for tok in _TOKEN_RE.findall(nline):
                if _is_valid_candidate(tok):
                    logger.debug("invoice_number_fallback: next-line hit %r on %r", tok, nline)
                    return tok

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

    # Fallback: broader header-region search for Nectar-style layouts where the
    # standard patterns miss the number (e.g. purely numeric ref on the same line
    # as the label but with unusual spacing / OCR noise).
    if not invoice_number:
        invoice_number = _invoice_number_fallback(text)
        if invoice_number:
            logger.debug("invoice_number extracted via fallback: %r", invoice_number)

    invoice_date_raw = first_match([
        r"invoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\bdate\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
    ], text)
    invoice_date = parse_date(invoice_date_raw)

    _curr = r"(?:EUR|GBP|USD|€|£|\$)?"
    net_raw = first_match([
        rf"(?:subtotal|sub[\s\-]?total|net[\s\-]?amount|amount\s+excl(?:uding)?\.?\s*(?:vat|tax)?|excl(?:uding)?\.?\s*(?:vat|tax)|net\s+total|taxable[\s\-]?amount|amount\s+before\s+(?:vat|tax))\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # Cash-sale / receipt style
        rf"(?:sub[\s\-]total|nett)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
    ], text)
    # VAT / tax patterns — covers V.A.T., VAT@rate%, IVA, tax amount, value added tax,
    # and standalone "vat" or "tax" followed by a currency amount.
    vat_raw = first_match([
        # Most specific: "V.A.T" / "VAT" with optional rate% prefix then amount
        rf"v\.?a\.?t\.?\s*(?:@\s*\d{{1,2}}\s*%\s*)?[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # value added tax / vat amount / tax amount
        rf"(?:value\s+added\s+tax|vat\s+amount|tax\s+amount)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # IVA (EU/IE terminology)
        rf"(?:iva|gst)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # Bare "tax" on a line with number following
        rf"(?:^|\n)\s*tax\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
    ], text)
    total_raw = first_match([
        # Specific multi-word labels first (more precise)
        rf"(?:amount due|balance due|grand total|total due|total amount|invoice total|total incl\.?\s*(?:vat|tax)?)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
        # Cash-sale / receipt style: "Total to Pay", "Total Payable", "Amount to Pay"
        rf"(?:total\s+to\s+pay|total\s+payable|amount\s+to\s+pay|amount\s+payable|total\s+inc(?:l(?:uding)?)?\.?\s*(?:vat|tax)?)\s*[:\-]?\s*{_curr}\s*([0-9.,]+)",
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

    # ── Structured summary override ───────────────────────────────────────────
    # For invoices with explicit Tax Analysis / Invoice Summary / Deposit Summary
    # blocks, use the structured parser to get more reliable net/vat/total figures.
    # Only override fields that the structured parser actually found.
    _struct = _extract_structured_summary_totals(text)
    if _struct:
        if _struct.get("net_amount") is not None:
            net_amount = _struct["net_amount"]
        if _struct.get("vat_amount") is not None:
            vat_amount = _struct["vat_amount"]
        if _struct.get("total_amount") is not None:
            total_amount = _struct["total_amount"]
        # _deposit_candidate is passed through the return dict for BCRS detection
        # in batches.py; it does NOT by itself trigger a split.

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

    # ── VAT recovery when TotalTax field is absent ──────────────────────────
    # Azure DI's TotalTax field can be unpopulated on some invoice layouts even
    # when VAT is clearly printed.  Two structured fallbacks recover the value
    # without any additional API calls:
    #
    # 1. Sum line-level Tax values — most accurate; each line carries its own
    #    tax amount which Azure DI often reads from the table body correctly.
    # 2. Implied arithmetic — total − net when the result is within a plausible
    #    VAT range (1 %–40 %) and both total & net were extracted confidently.
    if vat_amount is None and line_items:
        line_tax_total = sum(
            float(it["tax_amount"])
            for it in line_items
            if it.get("tax_amount") is not None and float(it["tax_amount"]) > 0
        )
        if line_tax_total >= 0.01:
            vat_amount = round(line_tax_total, 2)
            t_conf_tax = round(items_conf * 0.90, 2)
            logger.info(
                "Azure DI: VAT recovered from line-level Tax sum = %.2f", vat_amount
            )

    if vat_amount is None and net_amount is not None and total_amount is not None:
        implied_vat = round(float(total_amount) - float(net_amount), 2)
        net_f = float(net_amount)
        # Accept only if the implied rate is within a plausible range (1–40%)
        # and both figures were extracted with reasonable confidence.
        if (
            implied_vat > 0.01
            and net_f > 0
            and 0.01 <= implied_vat / net_f <= 0.40
            and t_conf_sub >= 0.50
            and t_conf_tot >= 0.50
        ):
            vat_amount = implied_vat
            t_conf_tax = round((t_conf_sub + t_conf_tot) / 2 * 0.75, 2)
            logger.info(
                "Azure DI: VAT inferred from total−net = %.2f (rate=%.1f%%)",
                vat_amount, implied_vat / net_f * 100,
            )

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
        # Full OCR text from Azure DI — used by the BCRS split logic in batches.py
        # for scanned/image pages where the native PDF text layer is absent.
        "di_page_text":     getattr(result, "content", None) or "",
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
    rule_supplier_ok = bool(
        merged.get("supplier_name")
        and not suspicious_supplier_name(merged.get("supplier_name"))
    )

    if ai_supplier and not suspicious_supplier_name(ai_supplier):
        if is_azure_di:
            # Azure DI uses a dedicated VendorName field from its prebuilt-invoice
            # model — a semantically trained slot, not a free-text scan.  Its
            # confidence score can be low for stylised logos / unusual fonts, but
            # the extracted value is almost always more reliable than position-
            # heuristic rule-based detection.
            # Policy: always prefer Azure DI's VendorName over rule-based,
            # even when rule-based found something plausible.  The account-company
            # hard-block above is the sufficient guard against the customer-name
            # confusion case.
            merged["supplier_name"] = ai_supplier
            if rule_supplier_ok and merged["supplier_name"] != ai_supplier:
                logger.info(
                    "merge_ai_fields: Azure DI VendorName '%s' overrides rule-based '%s'",
                    ai_supplier, merged.get("supplier_name"),
                )
        else:
            # OpenAI vision / text: free-form reading — keep the stricter gate
            # (≥ 0.85) to prevent picking up customer name printed elsewhere.
            if not rule_supplier_ok:
                merged["supplier_name"] = ai_supplier
            elif ai_supplier_conf >= 0.85:
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
    # Azure DI reads SubTotal / TotalTax / InvoiceTotal as dedicated semantic
    # fields from its prebuilt-invoice model and is significantly more reliable
    # than position-heuristic rule-based regex on multi-column invoice layouts.
    # Policy: if Azure DI returned a value, it REPLACES the rule-based value
    # (not just fills gaps).  OpenAI vision/text only fills gaps.
    if is_azure_di:
        if ai.get("net_amount") is not None:
            merged["net_amount"] = ai["net_amount"]
        if ai.get("vat_amount") is not None:
            merged["vat_amount"] = ai["vat_amount"]
        if ai.get("total_amount") is not None:
            merged["total_amount"] = ai["total_amount"]
    else:
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
        "di_page_text",            # full OCR text from Azure DI (used by BCRS detection)
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
    """Extract invoice data from a single PDF page.

    Pipeline stages
    ───────────────
    Stage 1 — Acquire + preprocess + quality score
        Render the page to JPEG, apply image enhancement (contrast boost,
        sharpness boost, bleed-through suppression) and compute a quality
        score (0.0–1.0) that feeds into later confidence blending.

    Stage 2 — Field extraction
        Rule-based pass (always) → Azure Document Intelligence (primary AI)
        → OpenAI vision (first fallback) → OpenAI text (second fallback).

    Stage 3 — Line normalization
        Net→Total fallback for zero-VAT invoices; deposit/BCRS component
        detection when totals don't reconcile; supplier name normalisation.

    Stage 4 — Accounting classification preparation
        Confidence scoring (rule-based + AI section scores + quality penalty),
        review reason code collection, validation_status assignment, and
        header/totals evidence strings for the UI.
    """
    pdf_path = Path(pdf_path)

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 1 — Document acquisition, preprocessing & quality assessment
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 1: acquiring page %d from %s", page_index, pdf_path.name)

    native_text = extract_native_pdf_page(pdf_path, page_index)
    method = "native_text"
    page_quality_score: float = 0.5  # default until we render the image

    use_vision = bool(settings.use_openai and openai_api_key)
    final_text = native_text

    if not use_vision:
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

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 2 — Field extraction
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 2: field extraction for page %d", page_index)

    # 2a — Rule-based baseline (no API cost, instant)
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
        # Render page → JPEG (shared by Azure DI and OpenAI vision).
        # Apply preprocessing to improve extraction accuracy on low-quality scans.
        try:
            raw_jpeg = OCRBackend.render_pdf_page_to_jpeg_bytes(
                pdf_path, page_index, scale=1.5, quality=80
            )
            if raw_jpeg and len(raw_jpeg) > 4 * 1024 * 1024:
                raw_jpeg = OCRBackend.render_pdf_page_to_jpeg_bytes(
                    pdf_path, page_index, scale=1.0, quality=60
                )
        except Exception as exc:
            logger.warning("JPEG render failed p%d: %s", page_index, exc)
            raw_jpeg = None

        # Stage 1 preprocessing applied once we have the JPEG
        if raw_jpeg:
            jpeg_bytes, page_quality_score = preprocess_page_image(raw_jpeg)
            logger.debug("Page %d quality score: %.2f", page_index, page_quality_score)
        else:
            jpeg_bytes = None

        ai_fields = None

        # 2b — Azure Document Intelligence (primary — highest accuracy)
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

        # 2c — OpenAI vision (fallback when Azure DI unavailable or returned nothing)
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

        # 2d — Text-only AI (second fallback when image unavailable)
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

        # 2e — OpenAI validation pass (cross-checks the merged result)
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

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 3 — Line normalization
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 3: line normalization for page %d", page_index)

    # 3a — Net → Total fallback for zero-VAT / subscription / reverse-charge invoices
    if (
        extracted.get("total_amount") is None
        and extracted.get("net_amount") is not None
        and not extracted.get("vat_amount")
    ):
        extracted["total_amount"] = extracted["net_amount"]
        logger.debug("Net→Total fallback applied: total set to %.2f", extracted["net_amount"])

    # 3b — Financial validation (replaces raw deposit-detection logic)
    #      Uses the dedicated validate_invoice module for component-aware
    #      reconciliation with structured reason codes.
    _inv_validation = None
    if _NEW_MODULES_AVAILABLE:
        try:
            _inv_validation = _validate_invoice(extracted)
            if _inv_validation.deposit_amount:
                extracted["_deposit_component"] = _inv_validation.deposit_amount
            extracted["_totals_reconciliation_status"] = (
                _inv_validation.totals_reconciliation_status
            )
            extracted["_validation_reasons"] = _inv_validation.review_reasons
        except Exception as _ve:
            logger.warning("validate_invoice failed on page %d: %s", page_index, _ve)
    else:
        # Fallback: legacy deposit detection
        net_s3  = extracted.get("net_amount")
        vat_s3  = extracted.get("vat_amount")
        tot_s3  = extracted.get("total_amount")
        if net_s3 is not None and tot_s3 is not None and vat_s3 is not None:
            diff_s3 = abs((float(net_s3) + float(vat_s3)) - float(tot_s3))
            if diff_s3 > 0.10:
                is_dep, dep_amt = _check_deposit_component(net_s3, vat_s3, tot_s3)
                if is_dep:
                    extracted["_deposit_component"] = dep_amt

    # 3c — Supplier name normalisation
    #      First apply the lightweight OCR artefact removal and casing fix,
    #      then run the full supplier normalisation module which adds
    #      match_method and canonical name from suppliers.yaml.
    raw_supplier = extracted.get("supplier_name")
    clean_supplier = normalise_company_name(
        _clean_ocr_supplier_name(raw_supplier)
    )
    extracted["supplier_name"] = clean_supplier
    extracted["_supplier_name_raw"] = raw_supplier or ""

    if _NEW_MODULES_AVAILABLE and clean_supplier:
        try:
            supplier_vat_s3 = extracted.get("supplier_vat")
            _snorm = _normalize_supplier(clean_supplier, supplier_vat=supplier_vat_s3)
            extracted["_supplier_norm"] = _snorm
            extracted["_supplier_match_method"] = _snorm.match_method
            # Prefer canonical name if we got a confident match
            if _snorm.match_method in ("vat_match", "alias_match") or \
               (_snorm.match_method == "fuzzy_match" and _snorm.match_confidence >= 0.75):
                extracted["supplier_name"] = _snorm.canonical
        except Exception as _sne:
            logger.warning("normalize_supplier failed on page %d: %s", page_index, _sne)

    # 3d — Date ambiguity detection
    #      Check if the extracted invoice_date is potentially ambiguous
    #      (day and month both ≤ 12, could be interpreted either way).
    if _NEW_MODULES_AVAILABLE:
        _raw_date_str = extracted.get("_invoice_date_raw") or str(
            extracted.get("invoice_date") or ""
        )
        if _raw_date_str:
            try:
                _pd = parse_invoice_date(_raw_date_str)
                extracted["_date_parse_strategy"] = _pd.parse_strategy
                extracted["_date_ambiguity_flag"] = _pd.ambiguity_flag
                if _pd.review_reason:
                    existing = extracted.get("_validation_reasons") or []
                    if _pd.review_reason not in existing:
                        extracted["_validation_reasons"] = existing + [_pd.review_reason]
            except Exception as _de:
                logger.debug("parse_invoice_date failed: %s", _de)

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 4 — Accounting classification preparation
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 4: accounting prep for page %d", page_index)

    # 4a — Confidence scoring
    ai_conf       = extracted.get("ai_confidence") or {}
    supplier_conf = float(ai_conf.get("supplier", 0.0))
    totals_conf   = float(ai_conf.get("totals", 0.0))

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
    if (
        extracted.get("net_amount") is not None
        and extracted.get("vat_amount") is not None
        and extracted.get("total_amount") is not None
        and round(
            (extracted["net_amount"] + extracted["vat_amount"]) - extracted["total_amount"], 2
        ) == 0
    ):
        rule_score = min(rule_score + 0.05, 1.0)

    if ai_conf:
        ai_overall = (
            supplier_conf * 0.35
            + totals_conf * 0.35
            + float(ai_conf.get("lines", 0.0)) * 0.15
            + float(ai_conf.get("customer", 0.0)) * 0.15
        )
        confidence = round(min(rule_score * 0.50 + ai_overall * 0.50, 0.99), 2)
    else:
        confidence = round(min(rule_score, 0.99), 2)

    # Apply a small quality penalty for very poor scans
    if page_quality_score < 0.35:
        confidence = round(max(confidence - 0.08, 0.0), 2)

    extracted["_confidence"] = confidence  # used by _collect_review_reasons

    # 4b — Validation pass result
    validation_result = extracted.pop("_validation_result", None)
    val_status = (
        validation_result.get("validated_status", "passed") if validation_result else None
    )
    val_issues = (validation_result.get("issues") or []) if validation_result else []

    # 4c — Review decision via the new review_engine (or fallback to legacy codes)
    _snorm = extracted.pop("_supplier_norm", None)
    _supplier_reasons: list[str] = []
    if _snorm is not None and _snorm.review_reason:
        _supplier_reasons.append(_snorm.review_reason)

    # Merge validation_reasons collected during Stage 3
    _val_reasons = extracted.pop("_validation_reasons", []) or []

    if _NEW_MODULES_AVAILABLE:
        try:
            review_decision = compute_review_decision(
                extracted=extracted,
                supplier_reasons=_supplier_reasons + _val_reasons,
                validation=extracted.pop("_inv_validation_obj", None),
                confidence=confidence,
                page_quality=page_quality_score,
            )
            review_reasons_list  = review_decision.review_reasons
            review_reasons_str   = "|".join(review_reasons_list) if review_reasons_list else None
            review_fields_str    = "|".join(review_decision.review_fields) if review_decision.review_fields else None
            review_priority_str  = review_decision.review_priority
            auto_approved        = review_decision.auto_approved
            final_status         = review_decision.validation_status
            review_required      = review_decision.review_required
        except Exception as _re_exc:
            logger.warning("compute_review_decision failed: %s", _re_exc)
            review_reasons_list = []
            review_reasons_str = review_fields_str = review_priority_str = None
            auto_approved = False
            final_status = "ok"
            review_required = False
    else:
        # Legacy fallback
        all_legacy = _collect_review_reasons(extracted, validation_result)
        all_legacy = list(dict.fromkeys(all_legacy + _supplier_reasons + _val_reasons))
        review_reasons_list = all_legacy
        review_reasons_str  = "|".join(all_legacy) if all_legacy else None
        review_fields_str   = None
        review_priority_str = None
        auto_approved       = False

        missing_supplier = not extracted.get("supplier_name")
        missing_amount   = extracted.get("total_amount") is None
        if val_status == "failed":
            final_status    = "review_validation_failed"
            review_required = True
        elif missing_supplier and missing_amount:
            final_status    = "review_incomplete"
            review_required = True
        elif missing_supplier:
            final_status    = "review_no_supplier"
            review_required = True
        elif missing_amount:
            final_status    = "review_no_amount"
            review_required = True
        elif any(r in all_legacy for r in ("totals_mismatch", "vat_anomaly")):
            final_status    = "review_validation_failed"
            review_required = True
        elif val_status == "passed_with_warnings" or "ai_validation_warned" in all_legacy:
            final_status    = "ok_warned"
            review_required = False
        elif confidence < 0.65:
            final_status    = "ok_warned"
            review_required = False
        else:
            final_status    = "ok"
            review_required = False

    extracted.pop("_confidence", None)

    # 4d — Merge all issues
    all_issues = list(extracted.get("ai_issues") or []) + val_issues
    if all_issues:
        extracted["ai_issues"] = all_issues

    if not extracted.get("extraction_status"):
        extracted["extraction_status"] = (
            "complete"        if confidence >= 0.80
            else "partial"    if confidence >= 0.50
            else "review_required"
        )

    # 4f — Build header/totals evidence strings for the review UI
    _text_is_unavailable = (
        not final_text
        or count_meaningful_chars(final_text) < 20
        or final_text.startswith("(page text unavailable")
    )

    if _text_is_unavailable:
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
        header_raw = (
            " | ".join(_header_parts) if _header_parts
            else f"[Scanned — extracted via {method}]"
        )
        _totals_parts: list[str] = []
        if extracted.get("net_amount") is not None:
            _totals_parts.append(f"Net: {extracted['net_amount']:.2f}")
        if extracted.get("vat_amount") is not None:
            _totals_parts.append(f"VAT: {extracted['vat_amount']:.2f}")
        if extracted.get("total_amount") is not None:
            _totals_parts.append(f"Total: {extracted['total_amount']:.2f}")
        if extracted.get("currency"):
            _totals_parts.append(f"Currency: {extracted['currency']}")
        totals_raw    = " | ".join(_totals_parts) if _totals_parts else None
        # For scanned/image pages, Azure DI returns result.content (full OCR text).
        # Use it for page_text_raw and totals_raw so the BCRS split logic in
        # batches.py can find labelled deposit/BCRS lines (e.g. "BCRS Deposit 2.40").
        _di_text = extracted.get("di_page_text") or ""
        if _di_text and len(_di_text.strip()) > 20:
            page_text_raw = _di_text[:20000]
            # Override totals_raw with the last lines of the DI text so BCRS labels
            # are available even when only the synthetic "Net: X | VAT: Y" was built.
            _di_lines = [ln.strip() for ln in _di_text.splitlines() if ln.strip()]
            if _di_lines:
                totals_raw = "\n".join(_di_lines[-15:])
        else:
            page_text_raw = f"[Scanned page — no text layer — extracted via {method}]\n" + header_raw
    else:
        header_raw    = "\n".join(final_text.splitlines()[:12])
        totals_raw    = "\n".join(final_text.splitlines()[-10:])
        page_text_raw = final_text[:20000]

    extracted.update({
        "page_no":                    page_index + 1,
        "method_used":                method,
        "confidence_score":           confidence,
        "validation_status":          final_status,
        "review_required":            review_required,
        "review_priority":            review_priority_str,
        "review_reasons":             review_reasons_str,
        "review_fields":              review_fields_str,
        "auto_approved":              auto_approved,
        "page_quality_score":         round(page_quality_score, 2),
        "supplier_match_method":      extracted.pop("_supplier_match_method", None),
        "totals_reconciliation_status": extracted.pop("_totals_reconciliation_status", None),
        "header_raw":                 header_raw,
        "totals_raw":                 totals_raw,
        "page_text_raw":              page_text_raw,
    })
    # Clean up internal temp keys
    for _k in ("_supplier_name_raw", "_date_parse_strategy", "_date_ambiguity_flag",
               "_deposit_component", "_validation_reasons", "_inv_validation_obj",
               "_supplier_norm", "_supplier_match_method", "_totals_reconciliation_status"):
        extracted.pop(_k, None)
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
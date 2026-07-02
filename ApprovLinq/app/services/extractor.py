from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import requests

from app.services.ocr import OCRBackend, OCRSpaceBackend, PaddleOCRBackend, TesseractOCRBackend
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
EXTRACTOR_BUILD_TAG = "phase8e_hotfix10c"


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def count_meaningful_chars(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]", text or ""))


def _header_region_lines(text: str, max_lines: int = 36) -> list[str]:
    lines = [" ".join((line or "").split()).strip() for line in (text or "").splitlines()]
    return [line for line in lines if line][:max_lines]


def _header_region_text(text: str, max_lines: int = 36) -> str:
    return clean_text("\n".join(_header_region_lines(text, max_lines=max_lines)))


def _totals_region_text(text: str, tail_lines: int = 28) -> str:
    lines = [" ".join((line or "").split()).strip() for line in (text or "").splitlines() if str(line or "").strip()]
    if not lines:
        return ""
    tail = lines[-tail_lines:]
    keep: list[str] = []
    amount_re = re.compile(r"(?:EUR|GBP|USD|€|£|\$)?\s*\d[\d,]*[.]\d{2}\b", re.I)
    label_re = re.compile(
        r"\b(?:subtotal|sub total|before tax|ex vat|net|vat|tax|total|grand total|amount due|balance due|"
        r"deposit|bcrs|contribution|refund(?:able)? deposit|total inc(?:l(?:uding)?)? vat)\b",
        re.I,
    )
    noise_re = re.compile(
        r"\b(?:terms|conditions|privacy|gdpr|thank you|website|www\.|http|email|tel|phone|fax|iban|swift|bic)\b",
        re.I,
    )
    for line in tail:
        if noise_re.search(line) and not label_re.search(line):
            continue
        if label_re.search(line) or amount_re.search(line):
            keep.append(line)
    return clean_text("\n".join(keep or tail[-12:]))


def _extract_supplier_vat_number(text: str | None) -> str | None:
    if not text:
        return None
    patterns = [
        r"\bvat\s*(?:no\.?|number|reg(?:istration)?\.?\s*no\.?)\s*[:\-]?\s*([A-Z]{0,2}\d{6,}[A-Z0-9]*)",
        r"\btax\s*(?:id|number|no\.?)\s*[:\-]?\s*([A-Z]{0,2}\d{6,}[A-Z0-9]*)",
        r"\b(MT\d{6,}[A-Z0-9]*)\b",
        r"\b([A-Z]{2}\d{6,}[A-Z0-9]*)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", "", match.group(1).upper())
        if 6 <= len(candidate) <= 20 and re.search(r"\d{6,}", candidate):
            return candidate
    return None


def _extract_invoice_date_value(text: str | None) -> str | None:
    if not text:
        return None
    patterns = [
        r"\binvoice\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\bdate\s*of\s*invoice\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\bdate\s*issued\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"\btax\s*point\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
        r"(?m)^(?!.*\bdue\b)(?!.*\bpayment\b)\s*date\s*[:\-]?\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})\s*$",
    ]
    return first_match(patterns, text)


def _extract_currency_code(text: str | None) -> str | None:
    raw = text or ""
    low = raw.lower()
    if "£" in raw or "gbp" in low:
        return "GBP"
    if "€" in raw or "eur" in low:
        return "EUR"
    if "$" in raw or "usd" in low:
        return "USD"
    return None


def _company_strength_score(value: str | None) -> int:
    v = " ".join(str(value or "").split()).strip()
    if not v or suspicious_supplier_name(v) or bad_supplier_line(v):
        return -100
    low = v.lower()
    score = 0
    words = re.findall(r"[A-Za-z0-9&'.-]+", v)
    score += min(len(words), 6) * 2
    score += min(len(v), 40) // 8
    if re.search(r"\b(ltd|limited|plc|company|co\.?|group|services|trading|holdings|international|bros|brothers)\b", low):
        score += 6
    if re.search(r"\d", v):
        score -= 3
    if re.search(r"\b(invoice|date|vat|customer|bill to|ship to|total|amount due)\b", low):
        score -= 12
    if re.search(r"\b(triq|trig|street|road|qormi|attard|marsa|birkirkara|malta)\b", low):
        score -= 10
    return score


def _amount_support_score(
    net_amount: float | None,
    vat_amount: float | None,
    total_amount: float | None,
    deposit_amount: float | None = None,
) -> int:
    try:
        net = float(net_amount) if net_amount is not None else None
        vat = float(vat_amount or 0) if net is not None else None
        total = float(total_amount) if total_amount is not None else None
        deposit = float(deposit_amount or 0) if deposit_amount is not None else 0.0
    except Exception:
        return 0
    if net is None or total is None:
        return 0
    diff_plain = abs((net + float(vat or 0)) - total)
    diff_with_deposit = abs((net + float(vat or 0) + float(deposit or 0)) - total)
    diff = min(diff_plain, diff_with_deposit if deposit_amount is not None else diff_plain)
    if diff <= 0.05:
        return 12
    if diff <= 0.15:
        return 8
    if diff <= 0.50:
        return 2
    return -6


def _prefer_base_amount(base_value: float | None, ai_value: float | None) -> bool:
    if base_value is None or ai_value is None:
        return False
    try:
        b = round(float(base_value), 2)
        a = round(float(ai_value), 2)
    except Exception:
        return False
    if b == a:
        return False
    if abs((b % 1) - (a % 1)) <= 0.001 and b > a and b >= 100 and a > 0:
        ratio = b / a
        if 1.5 <= ratio <= 20:
            return True
    return False


def preprocess_page_image(jpeg_bytes: bytes) -> tuple[bytes, float]:
    """Conservative image preprocessing for OCR/vision.

    On already-good pages, preserve the original render to avoid harming faint
    leading digits or crisp supplier names. On weaker pages, apply a light to
    moderate enhancement only.
    """
    try:
        import io
        import statistics
        from PIL import Image, ImageEnhance, ImageFilter

        img = Image.open(io.BytesIO(jpeg_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        gray = img.convert("L")
        pixels = list(gray.getdata())
        mean_px = sum(pixels) / max(len(pixels), 1)
        try:
            std_px = statistics.stdev(pixels)
        except statistics.StatisticsError:
            std_px = 0.0
        contrast_score = min(std_px / 75.0, 1.0)
        brightness_score = 1.0 - abs(mean_px - 128.0) / 128.0
        quality_score = round(contrast_score * 0.65 + brightness_score * 0.35, 2)

        if quality_score >= 0.72 and 105 <= mean_px <= 205:
            return jpeg_bytes, quality_score

        if quality_score >= 0.56:
            img = ImageEnhance.Contrast(img).enhance(1.10)
            img = ImageEnhance.Sharpness(img).enhance(1.05)
            if mean_px > 195:
                img = ImageEnhance.Brightness(img).enhance(0.96)
            elif mean_px < 88:
                img = ImageEnhance.Brightness(img).enhance(1.05)
            level = 1
        else:
            img = img.filter(ImageFilter.MedianFilter(size=3))
            img = ImageEnhance.Contrast(img).enhance(1.22)
            img = ImageEnhance.Sharpness(img).enhance(1.12)
            if mean_px > 185:
                img = ImageEnhance.Brightness(img).enhance(0.92)
            elif mean_px < 82:
                img = ImageEnhance.Brightness(img).enhance(1.10)
            level = 2

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        processed = buf.getvalue()
        logger.debug(
            "preprocess_page_image: level=%d quality=%.2f mean_px=%.0f input=%d bytes output=%d bytes",
            level, quality_score, mean_px, len(jpeg_bytes), len(processed),
        )
        return processed, quality_score
    except Exception as e:
        logger.warning("preprocess_page_image failed: %s", e)
        return jpeg_bytes, 0.5


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



# ── Finance-document remediation helpers ─────────────────────────────────────
def _money_values_from_line(line: str) -> list[float]:
    """Return monetary-looking values from a line, preserving left-to-right order.

    Deliberately ignores bare percentages such as the ``18`` in ``VAT 18% 525.18``.
    This is the core guard for Dione-style pages where OCR/DI sometimes returns
    the VAT rate as the VAT amount.
    """
    vals: list[float] = []
    for m in re.finditer(r"(?<![\d])(?:€\s*)?(-?\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2}|-?\d+[.,]\d{1,2})(?!\d)", line or ""):
        # Reject values that are immediately followed by a percent sign.
        tail = (line or "")[m.end():m.end()+2]
        if "%" in tail:
            continue
        parsed = parse_amount(m.group(1))
        if parsed is not None:
            vals.append(round(float(parsed), 2))
    return vals


def _last_money_on_label_line(text: str, label_patterns: list[str], reject_patterns: list[str] | None = None) -> float | None:
    """Find the last monetary value on the strongest matching labelled line."""
    reject_patterns = reject_patterns or []
    best: tuple[int, float] | None = None
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(re.search(p, low, re.I) for p in reject_patterns):
            continue
        if not any(re.search(p, low, re.I) for p in label_patterns):
            continue
        vals = _money_values_from_line(line)
        if not vals:
            # Some OCR engines split label and value on adjacent lines. Try a
            # conservative one-line lookahead, but never jump across another label.
            for nidx in range(idx + 1, min(idx + 2, len(lines))):
                nxt = lines[nidx]
                if re.search(r"\b(net|vat|tax|total|sub\s*total|gross|amount|bcrs|deposit)\b", nxt, re.I):
                    break
                vals = _money_values_from_line(nxt)
                if vals:
                    break
        if not vals:
            continue
        # Last money on a VAT line handles ``VAT 18% 525.18`` and
        # ``VAT € 18% 14.05 2.51`` correctly.
        score = 10
        if idx > len(lines) * 0.45:
            score += 2  # summary area preference
        val = vals[-1]
        if best is None or score >= best[0]:
            best = (score, val)
    return best[1] if best else None



def _summary_money_values_from_line(line: str) -> list[float]:
    """Money parser for summary rows, including OCR-split cents.

    OCR often turns ``40.50`` into ``40 50`` or ``98.88`` into ``98 88`` in
    summary blocks.  This helper is only used in labelled summary context; the
    general amount parser remains deliberately stricter for body tables.
    """
    vals = _money_values_from_line(line)
    occupied: list[tuple[int, int]] = []
    for m in re.finditer(r"(?<![\d])(?:€\s*)?(-?\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2}|-?\d+[.,]\d{1,2})(?!\d)", line or ""):
        occupied.append((m.start(), m.end()))
    for m in re.finditer(r"(?<!\d)(\d{1,4})\s+(\d{2})(?!\d)", line or ""):
        if any(not (m.end() <= a or m.start() >= b) for a, b in occupied):
            continue
        # Do not turn dates/times or VAT rates into amounts.
        left = (line or "")[max(0, m.start()-12):m.start()].lower()
        right = (line or "")[m.end():m.end()+8].lower()
        if "%" in right or re.search(r"\b(date|page|invoice\s*#?|no\.?|vat\s*(?:reg|no))\b", left):
            continue
        whole = int(m.group(1))
        cents = int(m.group(2))
        if whole == 0 and cents == 0:
            vals.append(0.0)
        elif whole > 0:
            vals.append(round(float(f"{whole}.{cents:02d}"), 2))
    for m in re.finditer(r"(?<!\d)(\d{1,4})\s+([0-9])(?!\d)", line or ""):
        if any(not (m.end() <= a or m.start() >= b) for a, b in occupied):
            continue
        left = (line or "")[max(0, m.start()-12):m.start()].lower()
        right = (line or "")[m.end():m.end()+8].lower()
        if "%" in right or re.search(r"\b(date|page|invoice\s*#?|no\.?|vat\s*(?:reg|no))\b", left):
            continue
        whole = int(m.group(1)); tenth = int(m.group(2))
        if whole > 0:
            vals.append(round(float(f"{whole}.{tenth}"), 2))
    # Preserve order but remove duplicates.
    out: list[float] = []
    for v in vals:
        if not any(abs(v - x) < 0.001 for x in out):
            out.append(v)
    return out


def _has_explicit_zero_vat(text: str) -> bool:
    low = text or ""
    # Restrict this to explicit VAT labels.  Generic "Tax 0.00" appears in
    # additional-charge columns on normal VAT invoices and must not zero out VAT.
    if re.search(r"\b(total\s+vat|vat\s+amt|vat\s+amount)\b[^\n]{0,20}(?:0[., ]00|0\.00|0,00)\b", low, re.I):
        return True
    lines = [ln.strip() for ln in low.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.search(r"\b(total\s+vat|vat\s+amt|vat\s+amount)\b", ln, re.I):
            for nxt in lines[i + 1:i + 3]:
                if re.search(r"^(?:0[., ]00|0\.00|0,00|0)$", nxt.strip(), re.I):
                    return True
                if re.search(r"\b(before\s+tax|total|due|amount|bank|iban|notes?)\b", nxt, re.I):
                    break
    return False



def _extract_vat_summary_row_bundle(text: str) -> dict[str, float]:
    """Parse compact VAT summary rows with net/VAT/gross columns.

    Handles generic layouts such as:
    - VAT Code Desc | Excl VAT | VAT | Total
    - VAT @ 18% Product 466.11 83.89 550.00
    - totals rows where OCR keeps the three accounting values on one line.

    This is deliberately label-driven and only accepts three values that
    reconcile as net + VAT = total.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln and ln.strip()]
    if not lines:
        return {}
    best: tuple[int, dict[str, float]] | None = None

    def _consider(vals: list[float], score: int) -> None:
        nonlocal best
        if len(vals) < 3:
            return
        # Use the right-most three money-like values.  In these summaries they
        # are normally Net/Excl, VAT, Gross/Total.
        net, vat, total = [round(float(x), 2) for x in vals[-3:]]
        if net < 0 or vat < 0 or total <= 0:
            return
        diff = abs(round((net + vat) - total, 2))
        if diff <= 0.03:
            cand = {"net_amount": net, "vat_amount": vat, "total_amount": total}
            final_score = score + 120
            if best is None or final_score > best[0]:
                best = (final_score, cand)

    for i, ln in enumerate(lines):
        low = ln.lower()
        # Header followed by value row.
        if re.search(r"\b(excl?\s*vat|ex\s*vat|excl?\.?\s*tax|before\s+tax)\b", low) and re.search(r"\b(vat|tax)\b", low) and re.search(r"\b(total|inc\s*vat|incl\s*vat|gross)\b", low):
            same = _summary_money_values_from_line(ln)
            _consider(same, 40 + (8 if i > len(lines) * 0.45 else 0))
            if len(same) < 3:
                for j in range(i + 1, min(len(lines), i + 4)):
                    nxt = lines[j]
                    if re.search(r"\b(bank|iban|signature|payment|client|account)\b", nxt, re.I):
                        break
                    vals = _summary_money_values_from_line(nxt)
                    if len(vals) >= 3:
                        _consider(vals, 45 + (8 if j > len(lines) * 0.45 else 0))
                        break
        # Compact row with VAT description and three trailing values.
        if re.search(r"\bvat\s*@?\s*(?:\d{1,2})\s*%|\bvat\s+(?:@|at)\b|\btax\s*@?\s*(?:\d{1,2})\s*%", low):
            vals = _summary_money_values_from_line(ln)
            _consider(vals, 42 + (8 if i > len(lines) * 0.45 else 0))

    return best[1] if best and best[0] >= 120 else {}

def _extract_summary_grid_bundle(text: str) -> dict[str, float]:
    """Extract invoice totals from labelled summary grids, not supplier names.

    Many invoices expose a bottom summary grid where OCR splits labels and
    amounts over several lines.  This parser only uses accounting labels and
    reconciliation, so it is batch/supplier independent.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln and ln.strip()]
    if not lines:
        return {}

    # Prefer bottom summary area.  Keep enough context for labels whose values
    # are split across adjacent lines.
    start = 0
    markers = [
        r"invoice\s+lines", r"additional\s+charges", r"total\s+(?:invoice|eur)",
        r"vat\s+rate", r"vat\s+code\s+desc", r"ex\s*va[ti]", r"amount\s+due", r"balance\s+due",
        r"grand\s+total", r"total\s+amount\s+to\s+pay",
    ]
    for i, ln in enumerate(lines):
        if any(re.search(p, ln, re.I) for p in markers):
            start = max(0, i - 3)
    seg = lines[start:]

    def label_values(label_re: str, *, stop_re: str | None = None, max_lookahead: int = 3) -> list[tuple[float, int, str]]:
        vals: list[tuple[float, int, str]] = []
        stop = re.compile(stop_re, re.I) if stop_re else None
        for i, ln in enumerate(seg):
            if not re.search(label_re, ln, re.I):
                continue
            same = _summary_money_values_from_line(ln)
            for v in same:
                vals.append((v, i, ln))
            if same:
                continue
            for j in range(i + 1, min(len(seg), i + 1 + max_lookahead)):
                nxt = seg[j]
                if stop and stop.search(nxt):
                    break
                if re.search(r"\b(code|description|qty|quantity|unit|retail|price|iban|bank|signature|notes?)\b", nxt, re.I):
                    continue
                ns = _summary_money_values_from_line(nxt)
                if ns:
                    for v in ns:
                        vals.append((v, i, f"{ln} -> {nxt}"))
                    break
        return vals

    # High-confidence explicit totals.
    explicit_total = label_values(
        r"\b(grand\s+total|total\s+amount\s+to\s+pay|amount\s+due|balance\s+due|total\s+due)\b|^\s*(?:due|dur|der|que|out|dun)\s*$",
        stop_re=r"\b(payment\s+options|bank|iban|notes?|signature|terms)\b",
        max_lookahead=2,
    )
    generic_total = label_values(
        r"^\s*total\s*[:\-]?$|^\s*total\s+[a-z€]*\s*[:\-]?|\btotal\s+(?:inc|incl|invoice|eur)\b",
        stop_re=r"\b(payment\s+total|payment\s+options|bank|iban|notes?|signature|terms|before\s+tax|vat|tax)\b",
        max_lookahead=2,
    )
    net_vals = label_values(
        r"\b(before\s+tax|before\s+[\"']?ax|net\s+amount|net\s+total|untaxed\s+amount|sub\s*total|subtotal|ex\s*va[ti]|total\s+excl)\b",
        stop_re=r"\b(tax|vat|total|payment|due|bank|iban|notes?)\b",
        max_lookahead=3,
    )
    vat_vals = label_values(
        r"\b(total\s+vat|vat\s+amt|vat\s+amount|tax\s+amount|value\s+added\s+tax)\b|^\s*(?:tax|ta[xr]|t\s*\*\*)\s*[:\-]?$",
        stop_re=r"\b(total|payment|due|bank|iban|notes?|before\s+tax)\b",
        max_lookahead=3,
    )

    # Filter obvious body/header values.
    net_candidates = [(v, 18 + (4 if i > len(seg) * 0.35 else 0), src) for v, i, src in net_vals if v >= 0]
    vat_candidates = [(v, 18 + (4 if i > len(seg) * 0.35 else 0), src) for v, i, src in vat_vals if v >= 0]
    total_candidates = []
    for v, i, src in explicit_total:
        if v > 0:
            # Full labels such as Amount Due / Balance Due are strong.  A bare
            # OCR line like "Dun"/"Que" is weaker because it is often a
            # distorted Due label and can carry a misread amount.
            bare_due = bool(re.match(r"^\s*(?:due|dur|der|que|out|dun)\s*$", src.split(" -> ", 1)[0], re.I))
            base = 12 if bare_due else 30
            total_candidates.append((v, base + (5 if i > len(seg) * 0.35 else 0), src))
    for v, i, src in generic_total:
        if v > 0:
            total_candidates.append((v, 16 + (4 if i > len(seg) * 0.35 else 0), src))

    # When VAT is explicitly zero, make zero available even if the OCR line was
    # not captured as a money value.
    if _has_explicit_zero_vat("\n".join(seg)):
        vat_candidates.append((0.0, 22, "explicit_zero_vat"))

    def dedupe(cands: list[tuple[float, int, str]]) -> list[tuple[float, int, str]]:
        best: dict[float, tuple[float, int, str]] = {}
        for val, score, src in cands:
            key = round(val, 2)
            if key not in best or score > best[key][1]:
                best[key] = (key, score, src)
        return list(best.values())

    net_candidates = dedupe(net_candidates)
    vat_candidates = dedupe(vat_candidates)
    total_candidates = dedupe(total_candidates)

    vat_row_bundle = _extract_vat_summary_row_bundle("\n".join(seg))
    if vat_row_bundle:
        for field, weight in (("net_amount", 38), ("vat_amount", 38), ("total_amount", 44)):
            val = vat_row_bundle.get(field)
            if val is None:
                continue
            src = "vat_summary_row"
            if field == "net_amount":
                net_candidates.append((round(val, 2), weight, src))
            elif field == "vat_amount":
                vat_candidates.append((round(val, 2), weight, src))
            elif field == "total_amount" and val > 0:
                total_candidates.append((round(val, 2), weight, src))
        net_candidates = dedupe(net_candidates)
        vat_candidates = dedupe(vat_candidates)
        total_candidates = dedupe(total_candidates)

    # Try accounting-consistent combinations first.
    best: tuple[int, dict[str, float]] | None = None
    for total, tw, tsrc in total_candidates:
        # Zero-VAT invoices: total is also net when explicit VAT amount is 0.
        if any(abs(v) <= 0.001 for v, _, _ in vat_candidates):
            score = tw + 60
            cand = {"net_amount": round(total, 2), "vat_amount": 0.0, "total_amount": round(total, 2)}
            if best is None or score > best[0]:
                best = (score, cand)
        for net, nw, nsrc in net_candidates or []:
            for vat, vw, vsrc in vat_candidates or [(round(total - net, 2), 8, "derived_vat")]:
                if net < 0 or vat < 0:
                    continue
                diff = abs(round((net + vat) - total, 2))
                score = tw + nw + vw
                if diff <= 0.03:
                    score += 120
                elif diff <= 0.10:
                    score += 90
                else:
                    # If net and total are strong, derive VAT for normal VAT invoices.
                    derived_vat = round(total - net, 2)
                    if derived_vat >= 0 and abs(derived_vat) <= max(total * 0.30, 0.01):
                        dscore = tw + nw + 125
                        dcand = {"net_amount": round(net, 2), "vat_amount": derived_vat, "total_amount": round(total, 2)}
                        if best is None or dscore > best[0]:
                            best = (dscore, dcand)
                    continue
                cand = {"net_amount": round(net, 2), "vat_amount": round(vat, 2), "total_amount": round(total, 2)}
                if best is None or score > best[0]:
                    best = (score, cand)
        # If total + VAT are known but net is missing, derive net if plausible.
        for vat, vw, vsrc in vat_candidates:
            if 0 <= vat < total:
                net = round(total - vat, 2)
                # Accept when VAT is a plausible rate or explicit zero.
                rate = (vat / net) if net else 0
                if abs(vat) <= 0.001 or 0.03 <= rate <= 0.30:
                    score = tw + vw + 75
                    has_positive_vat = any(_v > 0.001 for _v, _, _ in vat_candidates)
                    if vat > 0.001:
                        score += 12
                    elif has_positive_vat:
                        score -= 18
                    cand = {"net_amount": net, "vat_amount": round(vat, 2), "total_amount": round(total, 2)}
                    if best is None or score > best[0]:
                        best = (score, cand)

    if best and best[0] >= 70:
        return best[1]
    return {}

def _extract_labeled_financial_bundle(text: str) -> dict[str, float]:
    """Extract a reconciled net/VAT/total/deposit bundle from labelled summary text.

    This is intentionally deterministic and finance-specific. It runs over
    native text, Azure DI ``content`` and OCR text, and is used as a remediation
    layer when semantic DI fields are missing or inconsistent.
    """
    if not text or count_meaningful_chars(text) < 10:
        return {}

    net = _last_money_on_label_line(
        text,
        [
            r"\bnet\s+amount\b", r"\btotal\s+net\b", r"\bnet\s+total\b",
            r"\bsub\s*total\b", r"\bsubtotal\b", r"\btaxable\s+amount\b",
            r"\bamount\s+excl", r"\bexcl(?:uding)?\.?\s+(?:vat|tax)",
            r"\btotal\s+excl\.?\s*(?:vat|tax)\b",
        ],
        reject_patterns=[r"\bvat\b", r"\btax\s+amount\b", r"\bgross\b", r"\bincl"],
    )
    vat = _last_money_on_label_line(
        text,
        [
            r"\bv\.?a\.?t\.?\b", r"\bvat\s+amount\b", r"\btotal\s+vat\b",
            r"\btax\s+amount\b", r"\btax\s+total\b", r"\bvalue\s+added\s+tax\b",
        ],
        reject_patterns=[
            r"\bvat\s*(?:reg|no|number)\b", r"\bvat\s*summary\b",
            r"\b(total|amount)\s+inc(?:l|luding)?\.?\s+vat\b",
            r"\btotal\s+incl?\.?\s+vat\b",
            # Line-table headers such as "Total VAT Cons" are not the summary
            # VAT amount.  The actual VAT summary is handled by labelled-line
            # and reconciliation remediation below.
            r"\b(total\s+)?vat\s+(?:cons|code|type|rate|%)\b",
            r"\b(code|description|qty|quantity|unit|retail|cost|price|item)\b.*\bvat\b",
        ],
    )
    total = _last_money_on_label_line(
        text,
        [
            r"\binvoice\s+total\b", r"\bgrand\s+total\b", r"\btotal\s+gross\b",
            r"\btotal\s+amount\b", r"\btotal\s+amount\s+in\s+eur\b",
            r"\btotal\s+due\b", r"\bamount\s+due\b", r"\bbalance\s+due\b",
            r"\btotal\s+incl", r"^\s*total\s*[:\-]?$", r"^\s*total\s+[€a-z]*\s*[:\-]?",
            r"^\s*due\s*[:\-]?$", r"^\s*balance\s*[:\-]?$",
        ],
        reject_patterns=[r"\bsub\s*total\b", r"\bsubtotal\b", r"\btotal\s+net\b", r"\btotal\s+vat\b", r"\bvat\b", r"\btax\b", r"\bbcrs\b", r"\bdeposit\b"],
    )
    deposit = _last_money_on_label_line(
        text,
        [r"\bbcrs\b", r"\bdeposit\b", r"\breturnable", r"\breturnables?\b"],
        reject_patterns=[r"\bvat\b", r"\btax\b"],
    )

    out: dict[str, float] = {}
    if net is not None:
        out["net_amount"] = round(float(net), 2)
    if vat is not None:
        out["vat_amount"] = round(float(vat), 2)
    if total is not None:
        out["total_amount"] = round(float(total), 2)
    if deposit is not None and deposit > 0:
        out["_deposit_candidate"] = round(float(deposit), 2)

    # OCR often represents the lower summary grid as vertical labels followed
    # by values, e.g. N Calleja: "Net Amount" / "VAT Amount" / values on
    # following lines.  The normal same-line parser misses these.  Apply a
    # conservative vertical-summary repair and let reconciliation decide.
    vertical = _extract_vertical_summary_amounts(text)
    for k, v in vertical.items():
        if v is not None and (k not in out or out.get(k) in (None, 0.0)):
            out[k] = v

    # Bottom summary grids are usually the most reliable source for amounts.
    # Use them when they reconcile better than the generic labelled pass.
    grid = _extract_summary_grid_bundle(text)
    if grid:
        current_score = _bundle_support_score(out)
        grid_score = _bundle_support_score(grid)
        if grid_score >= current_score:
            for k, v in grid.items():
                if v is not None:
                    out[k] = v
        else:
            # Still use grid values to fill blanks.
            for k, v in grid.items():
                if v is not None and out.get(k) in (None, ""):
                    out[k] = v

    return _repair_financial_bundle(out, text)


def _extract_vertical_summary_amounts(text: str) -> dict[str, float]:
    """Parse label/value summary blocks where OCR puts labels and values on
    separate lines.  Kept deliberately narrow to avoid table-body leakage.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out: dict[str, float] = {}

    def _next_values(start: int, stop_labels: tuple[str, ...] = ()) -> list[float]:
        vals: list[float] = []
        for j in range(start + 1, min(len(lines), start + 8)):
            low = lines[j].lower()
            if any(re.search(p, low, re.I) for p in stop_labels):
                break
            # Avoid item-table rows: they usually contain several monetary values
            # together with qty/price/code context.
            if re.search(r"\b(code|description|qty|quantity|unit|price|item|retail|cost)\b", low):
                continue
            vals.extend(_summary_money_values_from_line(lines[j]))
            if vals and re.search(r"\b(delivered|received|payment|bank|iban|signature)\b", low, re.I):
                break
        return vals

    for i, line in enumerate(lines):
        low = line.lower()
        if re.search(r"^net\s+amount\b|^net\s+total\b|^total\s+net\b", low):
            vals = _summary_money_values_from_line(line) or _next_values(i, (r"^total", r"^invoice\s+total", r"^grand\s+total"))
            if vals:
                out.setdefault("net_amount", vals[0])
        elif re.search(r"^v\.?a\.?t\.?\s+amount\b|^vat\s*[:\-]?$|^total\s+vat\b", low):
            vals = _summary_money_values_from_line(line) or _next_values(i, (r"^total", r"^invoice\s+total", r"^grand\s+total", r"^bcrs", r"^deposit"))
            if vals:
                # Prefer an explicit zero/last value in VAT summary columns;
                # this fixes Nectar-like OCR "VAT: 93.75 0.00 E@0%".
                chosen = vals[-1]
                if any(abs(v) <= 0.001 for v in vals):
                    chosen = 0.0
                out.setdefault("vat_amount", round(float(chosen), 2))
        elif re.search(r"^total\s+amount\b|^total\s+due\b|^total\s+incl|^invoice\s+total\b|^grand\s+total\b", low):
            vals = _summary_money_values_from_line(line) or _next_values(i, (r"^bank\b", r"^iban\b", r"^signature\b"))
            if vals:
                out.setdefault("total_amount", vals[-1])
    return out


def _repair_financial_bundle(values: dict[str, float], text: str | None = None) -> dict[str, float]:
    """Apply small finance-safe repairs to labelled totals.

    Repairs are limited to obvious OCR/DI errors:
    - net and total are almost identical on a zero-VAT invoice but net lost a
      cent digit (67.9 vs 67.98) -> use the two-decimal total;
    - VAT equals the net/total on a zero-rated layout -> set VAT to 0.00;
    - when VAT and total are present but net is missing/duplicated from total,
      derive net = total - VAT if the implied VAT rate is plausible;
    - when an 18% VAT invoice has a bad VAT candidate from a table/body value,
      derive the summary net/VAT from the trusted invoice total.
    """
    out = dict(values or {})

    def _has_vat_rate(rate: int) -> bool:
        if rate == 18:
            return bool(re.search(r"(?:\b(?:vat|tax)\s*)?(?:18|1[8s]|[:;lI!|]8)\s*%", text or "", re.I))
        return bool(re.search(rf"\b(?:vat|tax)\s*{rate}\s*%|\b{rate}\s*%", text or "", re.I))

    def _derive_from_total_and_rate(total_f: float, rate: float) -> tuple[float, float]:
        net_f = round(total_f / (1.0 + rate), 2)
        vat_f = round(total_f - net_f, 2)
        return net_f, vat_f

    try:
        net = out.get("net_amount")
        vat = out.get("vat_amount")
        total = out.get("total_amount")
        net_f = round(float(net), 2) if net is not None else None
        vat_f = round(float(vat or 0.0), 2) if vat is not None else None
        total_f = round(float(total), 2) if total is not None else None

        # If the document only exposes a payable total and no VAT/tax breakdown,
        # treat it as a zero-VAT row so accounting export remains complete.
        # This is not supplier-specific: it applies to any total-only invoice
        # where there is no visible VAT percentage/amount summary.
        if total_f is not None and total_f > 0 and net_f is None and vat is None:
            has_tax_breakdown = bool(re.search(r"\b(total\s+vat|vat\s+amt|vat\s+amount|tax\s+amount|before\s+tax|ex\s*vat|excl?\s*vat|sub\s*total)\b", text or "", re.I))
            has_vat_rate = bool(re.search(r"\b(?:vat|tax)?\s*(?:5|7|18|19|20|21)\s*%", text or "", re.I))
            if not has_tax_breakdown and not has_vat_rate:
                out["net_amount"] = total_f
                out["vat_amount"] = 0.0
                net_f = total_f
                vat_f = 0.0

        if net_f is not None and total_f is not None:
            # If total and net are within a few cents on a zero VAT invoice, the
            # higher-confidence total line should repair a truncated net.
            if vat_f is not None and abs(vat_f) <= 0.001 and 0 < abs(total_f - net_f) <= 0.11:
                out["net_amount"] = total_f
                net_f = total_f
            # Nectar-style OCR: VAT summary line picks the net again as VAT.
            if vat is not None and abs(net_f - total_f) <= 0.05 and abs(float(vat) - total_f) <= 0.05:
                out["vat_amount"] = 0.0
                vat_f = 0.0

        # Explicit zero-VAT summary: total is the commercial net.  This
        # repairs invoices where OCR picked line contribution/discount columns
        # as net/VAT even though the summary says VAT is 0.00.
        if total_f is not None and _has_explicit_zero_vat(text or ""):
            out["vat_amount"] = 0.0
            out["net_amount"] = total_f
            net_f = total_f
            vat_f = 0.0

        # If VAT and total exist but net is absent or duplicated from the total
        # line, derive the commercial net. This fixes common PBL/Biocare OCR
        # summaries where the "Before Tax" value is missed.
        if total_f is not None and vat_f is not None and total_f > vat_f > 0:
            implied_net = round(total_f - vat_f, 2)
            implied_rate = vat_f / implied_net if implied_net > 0 else 0
            if 0.03 <= implied_rate <= 0.30 and (net_f is None or abs(net_f - total_f) <= 0.11):
                out["net_amount"] = implied_net
                net_f = implied_net

        # Some columnar invoices expose Tax and Gross under a "Sub Total"
        # label.  If the value currently sitting in net is a plausible VAT
        # component and VAT is absent, reinterpret it as VAT and derive net.
        if total_f is not None and net_f is not None and vat_f is None and total_f > net_f > 0:
            possible_net = round(total_f - net_f, 2)
            possible_rate = net_f / possible_net if possible_net > 0 else 0
            if 0.05 <= possible_rate <= 0.30 and re.search(r"\b(vat|tax|18\s*%|:8\s*%)", text or "", re.I):
                out["net_amount"] = possible_net
                out["vat_amount"] = net_f
                net_f, vat_f = possible_net, net_f

        # If the visible document carries an 18% VAT marker and the current
        # net/VAT/total bundle is missing or mismatched, derive VAT from the
        # trusted total. This is safer than accepting random body-table numbers
        # such as quantities, unit prices, or OCR-fragmented VAT values.
        if total_f is not None and total_f > 0 and _has_vat_rate(18):
            current_diff = None
            if net_f is not None and vat_f is not None:
                current_diff = abs(round((net_f + vat_f) - total_f, 2))
            if current_diff is None or current_diff > 0.10:
                rate_net, rate_vat = _derive_from_total_and_rate(total_f, 0.18)
                # Only apply when the derived values look like a normal 18% VAT
                # invoice and materially improve reconciliation.
                if rate_net > 0 and abs(round((rate_net + rate_vat) - total_f, 2)) <= 0.02:
                    out["net_amount"] = rate_net
                    out["vat_amount"] = rate_vat
                    net_f, vat_f = rate_net, rate_vat

        # If net and total exist but VAT is absent or implausible, derive VAT
        # from the two trusted summary values.
        if net_f is not None and total_f is not None and total_f >= net_f:
            derived_vat = round(total_f - net_f, 2)
            derived_rate = derived_vat / net_f if net_f > 0 else 0
            if derived_vat >= 0 and (vat_f is None or abs(round((net_f + vat_f) - total_f, 2)) > 0.10):
                if derived_vat == 0 or 0.03 <= derived_rate <= 0.30:
                    out["vat_amount"] = derived_vat
                    vat_f = derived_vat

        # If net and VAT exist but total is absent, fill total. If the current
        # total is clearly impossible (lower than net/VAT) and the expected total
        # appears in the OCR text, use the expected value as a guarded repair.
        if net_f is not None and vat_f is not None:
            expected_total = round(net_f + vat_f, 2)
            if total_f is None:
                out["total_amount"] = expected_total
            elif total_f < max(net_f, vat_f) and re.search(rf"(?<!\d){expected_total:.2f}(?!\d)", (text or "").replace(",", ".")):
                out["total_amount"] = expected_total
    except Exception:
        pass
    return out


def _bundle_support_score(values: dict[str, Any]) -> int:
    return _amount_support_score(
        values.get("net_amount"), values.get("vat_amount"), values.get("total_amount"), values.get("_deposit_candidate")
    )


def _apply_financial_remediation(extracted: dict[str, Any], text: str, source_tag: str) -> dict[str, Any]:
    """Repair missing/inconsistent financial fields from labelled text.

    The remediation is conservative:
    - fill missing values directly;
    - replace existing values only when the labelled bundle reconciles better;
    - protect VAT from rate-as-amount reads like 18 instead of 525.18;
    - preserve source metadata in ``_field_sources`` and method tags.
    """
    labelled = _extract_labeled_financial_bundle(text or "")
    if not labelled:
        return extracted

    current = dict(extracted)
    before_score = _bundle_support_score(current)
    candidate = dict(current)
    for k, v in labelled.items():
        if k in ("net_amount", "vat_amount", "total_amount", "_deposit_candidate"):
            candidate[k] = v
    after_score = _bundle_support_score(candidate)

    field_sources = dict(extracted.get("_field_sources") or {})

    def _set(field: str, value: float) -> None:
        old = extracted.get(field)
        if old != value:
            extracted[field] = value
            field_sources[field] = source_tag

    # Fill blanks first.
    for field in ("net_amount", "vat_amount", "total_amount"):
        if extracted.get(field) is None and labelled.get(field) is not None:
            _set(field, labelled[field])

    if labelled.get("_deposit_candidate") is not None and extracted.get("_deposit_candidate") is None:
        extracted["_deposit_candidate"] = labelled["_deposit_candidate"]

    # Replace when labelled fields reconcile materially better than current.
    if after_score >= before_score + 6:
        for field in ("net_amount", "vat_amount", "total_amount"):
            if labelled.get(field) is not None:
                _set(field, labelled[field])
    else:
        # Targeted VAT protection: if current VAT looks like a percentage rate or
        # causes a mismatch, and labelled VAT reconciles, repair VAT only.
        cur_vat = extracted.get("vat_amount")
        lab_vat = labelled.get("vat_amount")
        if lab_vat is not None and cur_vat is not None:
            try:
                cur_v = round(float(cur_vat), 2)
                lab_v = round(float(lab_vat), 2)
                net = float(extracted.get("net_amount") or labelled.get("net_amount") or 0)
                total = float(extracted.get("total_amount") or labelled.get("total_amount") or 0)
                cur_diff = abs((net + cur_v) - total)
                lab_diff = abs((net + lab_v) - total)
                if lab_v != cur_v and lab_diff + 0.05 < cur_diff:
                    _set("vat_amount", lab_v)
            except Exception:
                pass

    if field_sources:
        extracted["_field_sources"] = field_sources
        method = str(extracted.get("method_used") or "")
        if source_tag not in method:
            extracted["method_used"] = (method + "+" + source_tag).strip("+")
    return extracted


def _merge_text_recovery_fields(merged: dict[str, Any], recovery: dict[str, Any], source_tag: str) -> dict[str, Any]:
    """Use deterministic text recovery as a cascading remediation layer."""
    if not recovery:
        return merged
    field_sources = dict(merged.get("_field_sources") or {})

    # Supplier: fill only when missing/suspicious, or when recovery has a much
    # stronger company score.
    rec_supplier = normalise_company_name(_clean_ocr_supplier_name(recovery.get("supplier_name")))
    cur_supplier = normalise_company_name(_clean_ocr_supplier_name(merged.get("supplier_name")))
    if rec_supplier and not suspicious_supplier_name(rec_supplier):
        rec_score = _company_strength_score(rec_supplier)
        cur_score = _company_strength_score(cur_supplier)
        if not cur_supplier or suspicious_supplier_name(cur_supplier) or rec_score >= cur_score + 6:
            merged["supplier_name"] = rec_supplier
            field_sources["supplier_name"] = source_tag

    # Invoice number: fill when missing/suspicious.
    if recovery.get("invoice_number") and suspicious_invoice_number(merged.get("invoice_number")):
        merged["invoice_number"] = recovery.get("invoice_number")
        field_sources["invoice_number"] = source_tag

    # Amounts: apply the same reconciliation-aware repair to the recovery text.
    for field in ("net_amount", "vat_amount", "total_amount"):
        if merged.get(field) is None and recovery.get(field) is not None:
            merged[field] = recovery[field]
            field_sources[field] = source_tag
    if recovery.get("_deposit_candidate") is not None and merged.get("_deposit_candidate") is None:
        merged["_deposit_candidate"] = recovery.get("_deposit_candidate")

    if field_sources:
        merged["_field_sources"] = field_sources
        method = str(merged.get("method_used") or "")
        if source_tag not in method:
            merged["method_used"] = (method + "+" + source_tag).strip("+")
    return merged

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
        "%d-%B-%Y",
        "%d-%b-%Y",
        "%d.%B.%Y",
        "%d.%b.%Y",
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



def _invoice_text_signal_score(text: str | None) -> int:
    """Return a lightweight score for whether a PDF text layer looks useful for invoice extraction.

    Some image PDFs contain a misleading/empty native text layer.  A high raw
    character count alone is not enough: the text must contain invoice-like
    labels, monetary values, dates, or tax terms.  Low-score native text is used
    only as weak evidence and should trigger OCR/DI/vision fallback.
    """
    t = clean_text(text or "")
    if count_meaningful_chars(t) < 40:
        return 0
    low = t.lower()
    score = 0
    label_patterns = [
        r"\binvoice\b", r"\binv\.?\s*(no|number|#)?\b", r"\bvat\b",
        r"\btax\b", r"\btotal\b", r"\bsubtotal\b", r"\bnet\b",
        r"\bamount\b", r"\bbalance\b", r"\bdue\b", r"\bdate\b",
        r"\bsupplier\b", r"\bbill\s*to\b", r"\bpayment\b",
    ]
    for pat in label_patterns:
        if re.search(pat, low):
            score += 1
    # Money/date evidence is especially important for deciding whether native
    # text is good enough to avoid OCR.
    if re.search(r"(?:€|eur|usd|gbp|£|\$)?\s*\d{1,3}(?:[.,]\d{3})*[.,]\d{2}", t, re.I):
        score += 2
    if re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", t):
        score += 1
    if len([ln for ln in t.splitlines() if ln.strip()]) >= 4:
        score += 1
    return score


def _native_text_looks_usable(text: str | None) -> bool:
    """True when native PDF text should be trusted as the primary text source."""
    return count_meaningful_chars(text or "") >= 80 and _invoice_text_signal_score(text) >= 3


def _extraction_has_minimum_invoice_fields(extracted: dict[str, Any] | None) -> bool:
    """Detect whether deterministic extraction found enough to avoid OCR fallback.

    A supplier-only or text-only result is not enough.  We need at least a
    supplier/invoice/date clue and one monetary amount, or a sufficiently
    complete financial bundle.
    """
    if not extracted:
        return False
    has_identity = bool(extracted.get("supplier_name") or extracted.get("invoice_number") or extracted.get("invoice_date"))
    amount_count = sum(1 for k in ("net_amount", "vat_amount", "total_amount") if extracted.get(k) is not None)
    if has_identity and amount_count >= 1:
        return True
    return amount_count >= 2


def _fallback_extraction_is_usable(extracted: dict[str, Any] | None) -> bool:
    """Guard against OCR text fallback becoming a low-quality full extraction."""
    if not extracted:
        return False
    supplier = str(extracted.get("supplier_name") or "").strip().lower()
    if supplier in {"", "no supplier", "unknown", "n/a", "none"}:
        return False
    has_identity = bool(
        extracted.get("supplier_name")
        and (extracted.get("invoice_number") or extracted.get("invoice_date"))
    )
    has_amount = any(
        extracted.get(field) not in (None, "")
        for field in ("net_amount", "vat_amount", "total_amount")
    )
    return bool(has_identity and has_amount)


def _get_fallback_ocr_text(pdf_path: str | Path, page_index: int, native_text: str | None = None) -> tuple[str | None, str | None]:
    """Read a page with OCR when the native text layer is weak or non-invoice-like.

    Returns (text, method_tag).  The caller decides whether the OCR result is
    strong enough to replace native text.
    """
    global _ocr_fallback_error
    if _ocr_fallback_error:
        logger.warning("OCR fallback skipped for page %d: %s", page_index, _ocr_fallback_error)
        return None, None
    ocr_backend = get_ocr_backend()
    if ocr_backend is None:
        return None, None
    try:
        ocr_text = clean_text(
            ocr_backend.extract_text_from_pdf_page(
                pdf_path,
                page_index,
                scale=3.0 if getattr(ocr_backend, "name", "") == "tesseract" else 1.8,
            )
        )
        if not ocr_text:
            return None, None
        return ocr_text, f"ocr_{ocr_backend.name}"
    except Exception as e:
        if _ocr_error_is_batch_terminal(e):
            _ocr_fallback_error = f"OCR fallback disabled for this batch: {str(e)[:160]}"
        logger.warning("OCR fallback failed for page %d: %s", page_index, e)
        return None, None

def get_ocr_backend():
    if _ocr_fallback_error:
        return None
    provider = (settings.ocr_provider or "none").strip().lower()
    if provider == "ocr_space":
        return OCRSpaceBackend()
    if provider == "paddleocr":
        return PaddleOCRBackend()
    if provider in ("tesseract", "local_tesseract"):
        return TesseractOCRBackend()
    # Safe development/runtime fallback: when no external OCR provider is
    # configured, use local Tesseract if it is available.  This is only invoked
    # when the native PDF text layer is weak, so it does not slow digital PDFs.
    if provider in ("none", "", "auto"):
        try:
            return TesseractOCRBackend()
        except Exception:
            return None
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
    if v.endswith("/") or v.endswith("-"):
        return True
    # Pure-letter strings (no digits) are never real invoice numbers
    if re.match(r"^[A-Za-z\s]+$", v):
        return True
    digit_count = len(re.findall(r"\d", v))
    letter_count = len(re.findall(r"[a-z]", v))
    if digit_count < 3:
        return True
    # Short mixed header refs like "MP008" are often product/order references,
    # not the true invoice id. Keep longer prefixed ids such as INV10146.
    if letter_count >= 2 and digit_count <= 3 and len(v) <= 6:
        return True
    return False


def _invoice_candidate_quality_score(value: str | None, text: str | None = None) -> int:
    if not value:
        return -100
    raw = str(value).strip()
    low = raw.lower()
    if suspicious_invoice_number(raw):
        return -100
    digits = len(re.findall(r"\d", raw))
    letters = len(re.findall(r"[A-Za-z]", raw))
    score = 0
    score += min(digits, 10) * 2
    score += min(len(raw), 12)
    if re.fullmatch(r"\d{5,12}", raw):
        score += 8
    if re.fullmatch(r"[A-Za-z]{2,5}\d{4,10}", raw):
        score += 6
    if "/" in raw or "-" in raw:
        score -= 2
    if letters >= 2 and digits <= 4 and len(raw) <= 7:
        score -= 8
    if text:
        if re.search(
            rf"\b(?:invoice\s*(?:no\.?|number|#|nr\.?)|document\s*(?:no\.?|number)|inv(?:oice)?)\s*[.:\-]*\s*{re.escape(raw)}\b",
            text,
            re.I,
        ):
            score += 10
        elif re.search(rf"\b{re.escape(raw)}\b", text, re.I):
            score += 1
    return score


def _choose_best_invoice_candidate(candidates: list[str], text: str | None = None) -> str | None:
    ranked: list[tuple[int, int, str]] = []
    for idx, candidate in enumerate(candidates or []):
        cand = str(candidate or "").strip()
        if not cand:
            continue
        score = _invoice_candidate_quality_score(cand, text=text) - idx
        if score > -100:
            ranked.append((score, -idx, cand))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][2]


def _document_supplier_evidence_is_strong(
    supplier_name: str | None,
    supplier_vat: str | None = None,
    ai_confidence: dict[str, Any] | None = None,
) -> bool:
    if suspicious_supplier_name(supplier_name):
        return False
    score = _company_strength_score(supplier_name)
    supplier_conf = float((ai_confidence or {}).get("supplier", 0.0) or 0.0)
    if supplier_vat and score >= 4:
        return True
    if score >= 8:
        return True
    return score >= 6 and supplier_conf >= 0.50


_GENERIC_SUPPLIER_TOKENS = frozenset({
    "LTD", "LIMITED", "PLC", "LLC", "INC", "CO", "COMPANY", "GROUP",
    "TRADING", "SERVICES", "SERVICE", "HOLDINGS", "INTERNATIONAL",
    "AND", "THE", "OF", "FOR", "MALTA", "MT",
})


def _supplier_identity_tokens(value: str | None) -> tuple[str, ...]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z]{2,}", str(value or "").upper()):
        if token in _GENERIC_SUPPLIER_TOKENS:
            continue
        tokens.append(token)
    return tuple(tokens)


def _supplier_candidates_similar(a: str | None, b: str | None) -> bool:
    aa = normalise_company_name(_clean_ocr_supplier_name(a)) or ""
    bb = normalise_company_name(_clean_ocr_supplier_name(b)) or ""
    if not aa or not bb:
        return False
    if aa.lower() == bb.lower():
        return True
    at = _supplier_identity_tokens(aa)
    bt = _supplier_identity_tokens(bb)
    aset = set(at)
    bset = set(bt)
    if aset and bset:
        common = aset & bset
        if aset.issubset(bset) or bset.issubset(aset):
            return True
        if len(common) >= 2:
            return True
        if common and at and bt and (at[0] == bt[0] or at[-1] == bt[-1]):
            return True
    return SequenceMatcher(None, aa.lower(), bb.lower()).ratio() >= 0.84


def _supplier_source_weight(source: str, supplier_conf: float = 0.0) -> int:
    source_l = (source or "").strip().lower()
    if source_l == "azure_di_structured":
        return 16 + int(supplier_conf * 8)
    if source_l in {"header_supplier", "header_contact"}:
        return 14
    if source_l == "header_candidate":
        return 11
    if source_l == "full_text_candidate":
        return 6
    if source_l == "openai_ai":
        return 5 + int(supplier_conf * 5)
    return 4


def _supplier_candidate_is_actionable(candidate: dict[str, Any], supplier_vat: str | None = None) -> bool:
    norm = candidate.get("norm")
    source = str(candidate.get("representative_source") or candidate.get("source") or "")
    final_score = int(candidate.get("final_score") or 0)
    support = int(candidate.get("support") or 0)
    strength = int(candidate.get("strength") or 0)
    if norm is not None:
        if norm.match_method == "vat_match":
            return True
        if norm.match_method == "alias_match":
            return True
        if norm.match_method == "fuzzy_match" and float(norm.match_confidence or 0.0) >= 0.86:
            return True
    if supplier_vat and strength >= 5 and source in {"header_contact", "header_supplier", "azure_di_structured"}:
        return True
    if source == "header_contact" and final_score >= 26:
        return True
    if source in {"header_supplier", "azure_di_structured"} and support >= 1 and final_score >= 24:
        return True
    if source == "header_candidate" and support >= 2 and final_score >= 28:
        return True
    return False


def _resolve_supplier_identity(
    base: dict[str, Any],
    ai: dict[str, Any] | None,
    account_company_name: str | None = None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    account_tokens = _build_account_tokens(account_company_name)
    supplier_conf = float(((ai or {}).get("ai_confidence") or {}).get("supplier", 0.0) or 0.0)
    supplier_vat = (
        (ai or {}).get("supplier_vat")
        or base.get("supplier_vat")
        or _extract_supplier_vat_number(base.get("_header_text"))
        or _extract_supplier_vat_number((ai or {}).get("di_page_text"))
    )
    field_sources = dict(base.get("_field_sources") or {})
    raw_candidates: list[dict[str, Any]] = []

    def _blocked_by_account(candidate: str) -> bool:
        if not account_tokens:
            return False
        return any(re.search(r"\b" + re.escape(tok) + r"\b", candidate, re.I) for tok in account_tokens)

    def _add_candidate(value: str | None, source: str) -> None:
        cleaned = normalise_company_name(_clean_ocr_supplier_name(value))
        if not cleaned:
            return
        if suspicious_supplier_name(cleaned) or bad_supplier_line(cleaned):
            return
        if _blocked_by_account(cleaned):
            return
        strength = _company_strength_score(cleaned)
        if strength < 0:
            return
        score = strength + _supplier_source_weight(source, supplier_conf=supplier_conf)
        norm = None
        if _NEW_MODULES_AVAILABLE:
            try:
                norm = _normalize_supplier(cleaned, supplier_vat=supplier_vat)
            except Exception:
                norm = None
        if norm is not None:
            if norm.match_method == "vat_match":
                score += 40
            elif norm.match_method == "alias_match":
                score += 26
            elif norm.match_method == "fuzzy_match":
                score += 8 + int(float(norm.match_confidence or 0.0) * 8)
        raw_candidates.append({
            "value": cleaned,
            "source": source,
            "score": score,
            "strength": strength,
            "norm": norm,
        })

    base_supplier = base.get("supplier_name")
    if base_supplier:
        _add_candidate(base_supplier, field_sources.get("supplier_name") or "header_supplier")
    for candidate in base.get("_supplier_candidates") or []:
        _add_candidate(candidate, "header_candidate")

    header_text = base.get("_supplier_header_text") or base.get("_header_text") or ""
    if header_text:
        for candidate in _collect_supplier_candidates(header_text, account_tokens=account_tokens):
            _add_candidate(candidate, "header_supplier")
        _add_candidate(find_supplier_name(header_text, account_tokens=account_tokens), "header_contact")

    if ai:
        ai_source = "azure_di_structured" if ai.get("extraction_source") == "azure_di" else "openai_ai"
        _add_candidate(ai.get("supplier_name"), ai_source)
        di_fields = ai.get("_di_structured_fields") or {}
        _add_candidate(di_fields.get("supplier_name"), "azure_di_structured")
        di_header_text = _header_region_text(ai.get("di_page_text") or "", max_lines=18)
        if di_header_text:
            _add_candidate(find_supplier_name(di_header_text, account_tokens=account_tokens), "header_contact")
            for candidate in _collect_supplier_candidates(di_header_text, account_tokens=account_tokens):
                _add_candidate(candidate, "header_candidate")

    if not raw_candidates:
        return None, None, {"support": 0, "score": 0, "source": None}

    ranked: list[dict[str, Any]] = []
    for idx, candidate in enumerate(raw_candidates):
        support = 0
        representative = candidate["value"]
        representative_source = candidate["source"]
        representative_score = candidate["score"]
        for jdx, other in enumerate(raw_candidates):
            if idx == jdx:
                continue
            if not _supplier_candidates_similar(candidate["value"], other["value"]):
                continue
            support += 1
            other_value = other["value"]
            other_score = other["score"]
            if (
                len(other_value) > len(representative) + 2
                and other_score >= representative_score - 2
            ) or other_score > representative_score + 2:
                representative = other_value
                representative_source = other["source"]
                representative_score = other_score
        ranked.append({
            **candidate,
            "support": support,
            "representative": representative,
            "representative_source": representative_source,
            "final_score": candidate["score"] + (support * 6) + min(len(representative), 36) // 12,
        })

    ranked.sort(
        key=lambda item: (
            item["final_score"],
            item["support"],
            item["strength"],
            len(item["representative"]),
        ),
        reverse=True,
    )
    best = ranked[0]
    if not _supplier_candidate_is_actionable(best, supplier_vat=supplier_vat):
        return None, None, {"support": best["support"], "score": best["final_score"], "source": best["representative_source"]}
    return best["representative"], best["representative_source"], {
        "support": best["support"],
        "score": best["final_score"],
        "source": best["representative_source"],
    }


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
        r"@",
        r"\b[a-z]\s*:\s*\+?\d",
        r"www\.",
        r"http",
        r"tel",
        r"phone",
        r"mobile",
        r"fax",
        r"bcrs",
        r"deposit",
        r"gross",
        r"net\s+total",
        r"total\s+net",
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
        r"@",
        r"\b[a-z]\s*:\s*\+?\d",
        r"\bwww\.",
        r"\bhttp",
        r"\btel\b",
        r"\bphone\b",
        r"\bmobile\b",
        r"\bfax\b",
        r"\bcustomer\b",
        r"\bbill to\b",
        r"\bship to\b",
        r"\bbcrs\b",
        r"\bdeposit\b",
        r"\bnet\s+total\b",
        r"\btotal\s+net\b",
        r"\bgross\b",
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
        r"\btrig\b",                # common OCR for Triq
        r"^\s*[a-z]\s*[:\-]\s*trig\b",
        r"^\s*[a-z]\s*[:\-]\s*triq\b",
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
        # Walk backward up to 10 lines looking for plausible company names.
        # Do not simply return the nearest line: OCR often places an address or
        # tagline immediately above Tel/Email, while the actual legal name is a
        # few lines higher (e.g. "Mafimex Ltd." above "The Fruit and Vegetable Centre").
        candidates: list[tuple[int, str]] = []
        for i in range(contact_idx - 1, max(-1, contact_idx - 10), -1):
            candidate = lines[i]
            if _is_plausible_company(candidate):
                score = _company_strength_score(candidate)
                if re.search(r"\b(ltd|limited|plc|llc|inc|company|co\.?)\b", candidate, re.I):
                    score += 14
                if i <= 3:
                    score += 5
                # Taglines/product descriptors can be plausible text, but should
                # lose to a nearby legal-name line.
                if re.search(r"\b(centre|center|fruit|vegetable|fresh|quality|services?)\b", candidate, re.I) and not re.search(r"\b(ltd|limited|plc|llc|inc)\b", candidate, re.I):
                    score -= 6
                candidates.append((score, candidate))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
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
        next_is_transaction_heading = bool(re.search(r"\b(cash\s+sale|invoice|tax\s+invoice|delivery\s+note|receipt)\b", next_line, re.I))
        if (
            i + 1 < len(header_lines)
            and i not in customer_section_indices
            and (i + 1) not in customer_section_indices
            and len(line) <= 20
            and len(next_line) <= 20
            and not bad_supplier_line(line)
            and not bad_supplier_line(next_line)
            and not next_is_address
            and not next_is_transaction_heading
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

    if not best:
        return None
    cleaned_best = normalise_company_name(_clean_ocr_supplier_name(best)) or best
    return cleaned_best[:200]


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


def _collapse_ws(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", str(value)).strip()
    return collapsed or None


def _clean_di_supplier_name(value: str | None) -> str | None:
    """Conservatively clean DI supplier names without supplier-specific rules."""
    text = _collapse_ws(value)
    if not text:
        return None
    # Strip obvious logo/acronym crumbs before a real company-looking token.
    # Do not strip Titlecase first words such as "Cafe Roma".
    text = re.sub(r"^(?:[a-z]{2,5}|[A-Z]{2,5}|[0-9]{1,3}|[&+])\s+(?=[A-Z][A-Za-z]{3,}\b)", "", text).strip()
    text = re.sub(r"^[^A-Za-z]+(?=[A-Za-z])", "", text).strip()
    text = re.sub(
        r"^([A-Za-z][A-Za-z&.'-]*(?:\s+[A-Za-z][A-Za-z&.'-]*){0,2})\s+\1\b",
        r"\1",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b([A-Za-z][A-Za-z&.'-]*(?:\s+[A-Za-z][A-Za-z&.'-]*){1,2})\s+\1\b", r"\1", text, flags=re.I)
    text = re.sub(r"\b([A-Za-z]{2,})\s+\1\b", r"\1", text, flags=re.I)
    words = text.split()
    if len(words) >= 4:
        last = re.sub(r"[^A-Za-z0-9]", "", words[-1]).lower()
        prior = [re.sub(r"[^A-Za-z0-9]", "", w).lower() for w in words[:-1]]
        has_suffix = any(w in {"co", "ltd", "limited", "plc", "company"} for w in prior)
        if has_suffix and last and last in prior:
            text = " ".join(words[:-1])
    text = _clean_ocr_supplier_name(text) or text
    text = re.sub(r"\s+", " ", text).strip(" -;,")
    return text or None


def _di_field_content_text(field: Any) -> str | None:
    if field is None:
        return None
    try:
        content = getattr(field, "content", None)
    except Exception:
        content = None
    if content is None and isinstance(field, dict):
        content = field.get("content")
    return _collapse_ws(content)


def _di_direct_field_value(field: Any) -> Any:
    """Match the standalone DI dump script: return raw DI content first."""
    if not isinstance(field, dict):
        return None
    for key in (
        "content",
        "value_string",
        "value_date",
        "value_time",
        "value_phone_number",
        "value_number",
        "value_integer",
        "value_currency",
        "value_address",
        "value_selection_mark",
    ):
        if key in field and field.get(key) not in (None, ""):
            return field.get(key)
    if isinstance(field.get("value_array"), list):
        return f"[array: {len(field['value_array'])}]"
    if isinstance(field.get("value_object"), dict):
        return f"[object: {len(field['value_object'])} keys]"
    return None


def _di_direct_text(field: Any) -> str | None:
    value = _di_direct_field_value(field)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return _collapse_ws(value)


_DIRECT_FIELD_TO_DI_FIELD = {
    "supplier_name": "VendorName",
    "invoice_number": "InvoiceId",
    "invoice_date": "InvoiceDate",
    "due_date": "DueDate",
    "net_amount": "SubTotal",
    "vat_amount": "TotalTax",
    "total_amount": "InvoiceTotal",
    "currency": "CurrencyCode",
    "customer_name": "CustomerName",
    "customer_vat": "CustomerTaxId",
    "supplier_vat": "VendorTaxId",
    "purchase_order": "PurchaseOrder",
    "order_number": "OrderNumber",
    "description": "Items",
}


def _value_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _candidate_value_for_field(field_name: str, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _candidate_confidence(source_payload: dict[str, Any], field_name: str) -> float | None:
    conf = ((source_payload.get("ai_confidence") or {}) if isinstance(source_payload, dict) else {}).get(
        "totals" if field_name in {"net_amount", "vat_amount", "total_amount"} else "supplier"
    )
    try:
        return float(conf) if conf is not None else None
    except Exception:
        return None


def _append_field_candidate(
    candidates: list[dict[str, Any]],
    *,
    field_name: str,
    value: Any,
    source_type: str,
    source_id: str,
    confidence: float | None,
    evidence: str | None,
    reason: str,
    selected: bool,
    applied: bool,
    conflict: bool = False,
) -> None:
    candidate_value = _candidate_value_for_field(field_name, value)
    if candidate_value is None:
        return
    candidates.append({
        "field_name": field_name,
        "candidate_value": candidate_value,
        "normalised_value": candidate_value,
        "source_type": source_type,
        "source_id": source_id,
        "confidence": confidence,
        "evidence": (evidence or candidate_value)[:2000],
        "reason": reason,
        "selected": selected,
        "applied": applied,
        "conflict": conflict,
    })


def _apply_direct_di_content_blank_fallbacks(
    row: dict[str, Any],
    *,
    page_text: str | None,
    account_company_name: str | None,
) -> None:
    """Fill blank direct-DI fields from Azure DI's own full page text.

    This is intentionally not a second provider call.  The prebuilt-invoice
    response often has complete OCR text even when a semantic field such as
    InvoiceTotal or InvoiceId is blank.  Use that text before native/OCR/AI
    fallback, and only fill fields that DI did not already supply.
    """
    if not page_text or count_meaningful_chars(page_text) < 10:
        return

    blank_fields = [
        field_name
        for field_name in (
            "supplier_name", "invoice_number", "invoice_date", "net_amount",
            "vat_amount", "total_amount", "currency", "description",
        )
        if _value_blank(row.get(field_name))
    ]
    if not blank_fields:
        return

    fallback_payload = simple_extract(
        page_text,
        openai_api_key=None,
        account_company_name=account_company_name,
    )
    fallback_payload["page_text_raw"] = page_text
    if not any(not _value_blank(fallback_payload.get(field_name)) for field_name in blank_fields):
        return

    candidates: list[dict[str, Any]] = list(row.get("_field_candidates") or [])
    filled_critical: list[str] = list(row.get("_field_fallback_critical_fields") or [])
    field_sources = dict(row.get("_direct_di_field_sources") or {})
    changed = False

    for field_name in blank_fields:
        value = fallback_payload.get(field_name)
        if _value_blank(value):
            continue
        if field_name == "supplier_name":
            value = normalise_company_name(_clean_ocr_supplier_name(str(value)))
        if field_name == "invoice_date":
            value = parse_date(str(value)) if value is not None else None
        if field_name in {"net_amount", "vat_amount", "total_amount"}:
            value = parse_amount(str(value))
        if _value_blank(value):
            continue

        row[field_name] = value
        field_sources[field_name] = "azure_di_content_text_fallback"
        changed = True
        if field_name in {"supplier_name", "invoice_number", "invoice_date", "total_amount"}:
            filled_critical.append(field_name)
        _append_field_candidate(
            candidates,
            field_name=field_name,
            value=value,
            source_type="di_text_field_fallback",
            source_id="azure_di_content",
            confidence=_candidate_confidence(fallback_payload, field_name),
            evidence=(fallback_payload.get("_header_text") or fallback_payload.get("_totals_text") or page_text or "")[:2000],
            reason="filled_blank_from_direct_di_content",
            selected=True,
            applied=True,
        )

    if changed:
        row["_field_candidates"] = candidates
        row["_field_fallback_critical_fields"] = sorted(set(filled_critical))
        row["_direct_di_field_sources"] = field_sources
        row["_provider_status"] = "di_success_field_fallback"
        row["_fallback_used"] = True
        method = str(row.get("method_used") or "DI")
        if "DI_CONTENT_TEXT_FALLBACK" not in method:
            row["method_used"] = method + "+DI_CONTENT_TEXT_FALLBACK"
        source = str(row.get("extraction_source") or "azure_di_direct")
        if "azure_di_content_text_fallback" not in source:
            row["extraction_source"] = source + "+azure_di_content_text_fallback"


def _apply_direct_blank_field_fallbacks(
    row: dict[str, Any],
    *,
    pdf_path: str | Path,
    page_index: int,
    native_text: str | None,
    openai_api_key: str | None,
    account_company_name: str | None,
) -> None:
    blank_fields = [
        field_name
        for field_name in (
            "supplier_name", "invoice_number", "invoice_date", "net_amount",
            "vat_amount", "total_amount", "currency", "description",
        )
        if _value_blank(row.get(field_name))
    ]
    candidates: list[dict[str, Any]] = list(row.get("_field_candidates") or [])
    fallback_text = None
    fallback_method = None
    fallback_payload: dict[str, Any] | None = None
    ai_payload: dict[str, Any] | None = None
    if blank_fields:
        fallback_text, fallback_method = _get_fallback_ocr_text(pdf_path, page_index, native_text)
        if fallback_text and count_meaningful_chars(fallback_text) >= 10:
            fallback_payload = simple_extract(
                fallback_text,
                openai_api_key=None,
                account_company_name=account_company_name,
            )
            fallback_payload["page_text_raw"] = fallback_text

        if openai_api_key and fallback_text and count_meaningful_chars(fallback_text) >= 20:
            ai_payload = openai_extract_invoice_fields(
                fallback_text,
                openai_api_key,
                model=settings.openai_model,
                account_company_name=account_company_name,
            )

    filled_critical: list[str] = list(row.get("_field_fallback_critical_fields") or [])
    fallback_sources = [
        ("ocr_field_fallback", fallback_method or "ocr_fallback", fallback_payload),
        ("ai_field_fallback", "openai_text", ai_payload),
    ]
    for field_name in blank_fields:
        for source_type, source_id, source_payload in fallback_sources:
            if not source_payload:
                continue
            value = source_payload.get(field_name)
            if _value_blank(value):
                continue
            row[field_name] = value
            if field_name in {"supplier_name", "invoice_number", "invoice_date", "total_amount"}:
                filled_critical.append(field_name)
            _append_field_candidate(
                candidates,
                field_name=field_name,
                value=value,
                source_type=source_type,
                source_id=source_id,
                confidence=_candidate_confidence(source_payload, field_name),
                evidence=(source_payload.get("_header_text") or source_payload.get("_totals_text") or fallback_text or "")[:2000],
                reason="filled_blank_after_direct_di",
                selected=True,
                applied=True,
            )
            break

    row["_field_candidates"] = candidates
    row["_provider_status"] = "di_success_field_fallback" if candidates else ("di_success_with_blanks" if blank_fields else "di_success")
    row["_fallback_used"] = bool(candidates)
    review_reasons: list[str] = []
    if filled_critical:
        review_reasons.append("fallback_used_for_critical_field")
    missing_core = [
        field_name
        for field_name in ("supplier_name", "invoice_number", "invoice_date", "total_amount")
        if _value_blank(row.get(field_name))
    ]
    if missing_core:
        review_reasons.append("missing_required_field")
    try:
        conf = row.get("confidence_score")
        if conf is not None and float(conf) < 0.60:
            review_reasons.append("low_confidence")
    except Exception:
        pass
    if row.get("_di_raw_invoice_date") and row.get("invoice_date") is None:
        review_reasons.append("invalid_date")
    try:
        net = row.get("net_amount")
        vat = row.get("vat_amount")
        total = row.get("total_amount")
        if net is not None and vat is not None and total is not None:
            if abs((float(net) + float(vat)) - float(total)) > 0.03:
                review_reasons.append("total_mismatch")
    except Exception:
        review_reasons.append("total_mismatch")
    if review_reasons:
        row["review_required"] = True
        row["auto_approved"] = False
        row["validation_status"] = "review_" + review_reasons[0]
        row["review_reasons"] = "|".join(dict.fromkeys(review_reasons))
        row["review_fields"] = "|".join(sorted(set(filled_critical + missing_core)))


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
    # Strip leading logo/anniversary artefacts before the real supplier name.
    # Examples seen on invoices:
    #   "35 Nectar Limited"      -> "Nectar Limited"
    #   "35 nectar limited"      -> "nectar limited"
    #   "5 J.Sultana"            -> "J.Sultana"
    #   "jbl Joseph Borg Ltd."   -> "Joseph Borg Ltd."
    # Keep true numeric brands such as "3M" by only removing an isolated
    # numeric/logo token when followed by a normal alphabetic word.
    name = re.sub(r"^\s*(?:since\s*)?\d{1,3}(?:\s*(?:years?|yrs?|anniversary|since))?[\s:|\-–—]+(?=[A-Za-z]{4,}\b)", "", name, flags=re.I)
    # Strip leading non-letter junk (symbols, short lowercase OCR words) before
    # the first real alphabetic company token.
    name = re.sub(r"^[^A-Za-z]+(?=[A-Za-z])", "", name)
    # Some OCR runs prepend logo crumbs such as "nector*" or "Pw" before a
    # legal title on the same crop.  Remove a single short lowercase/noisy token
    # if it is followed by a stronger legal/company-looking phrase.
    name = re.sub(r"^\s*[a-z0-9*]{1,8}\s+(?=[A-Z][A-Za-z&.' -]{2,}\b(?:ltd|limited|plc|co\.?|company)\b)", "", name)
    # If the name starts with "X Y..." where X is a single uppercase char and Y
    # begins with the same letter (OCR duplicated initial), strip the lone prefix char.
    # e.g. "N N Calleja Trading" → "N Calleja Trading"
    m = re.match(r"^([A-Z])\s+([A-Z]\S.*)$", name)
    if m and m.group(2).upper().startswith(m.group(1)):
        name = m.group(2)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    # If a remap/DI crop glued the supplier to a transactional heading, keep
    # the legal title only: "Nectar Limited Cash sale" -> "Nectar Limited".
    m_title = re.match(r"^(.{2,80}?\b(?:ltd|limited|plc|llc|inc|company|co\.?)\.?)\b(?:\s+(?:cash\s+sale|invoice|tax\s+invoice|sales\s+invoice|page\b).*)?$", name, re.I)
    if m_title:
        name = m_title.group(1).strip()
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


def _dedupe_candidates(values: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not v:
            continue
        vv = " ".join(str(v).split()).strip()
        key = vv.lower()
        if not vv or key in seen:
            continue
        seen.add(key)
        out.append(vv)
    return out


def _rank_candidates_with_llm(field_name: str, candidates: list[str], page_text: str, api_key: str | None, model: str = "gpt-4.1-mini") -> dict[str, Any] | None:
    if not api_key or len(candidates) < 2:
        return None
    prompt = (
        f"Choose the best {field_name} candidate from the list. Return JSON only with keys chosen_candidate, confidence, review_recommended, reason. "
        f"You must choose one of the provided candidates exactly or null. Field: {field_name}.\n"
        f"Candidates: {json.dumps(candidates)}\n"
        f"Page text excerpt:\n{page_text[:3500]}"
    )
    raw = _call_openai(prompt, api_key, model, max_tokens=180, timeout=35)
    if not raw:
        return None
    try:
        m = re.search(r"\?\{.*\}", raw, re.S)
    except Exception:
        m = None
    try:
        payload = json.loads(m.group(0) if m else raw)
        chosen = payload.get("chosen_candidate")
        if chosen is not None and chosen not in candidates:
            return None
        return payload
    except Exception:
        return None


def _collect_supplier_candidates(text: str, account_tokens: list[str] | None = None) -> list[str]:
    candidates: list[str] = []
    account_tokens = account_tokens or frozenset()
    primary = find_supplier_name(text, account_tokens=account_tokens)
    if primary:
        candidates.append(primary)
    for ln in text.splitlines()[:14]:
        ln = normalise_company_name(_clean_ocr_supplier_name(" ".join(ln.split()).strip()))
        if not ln:
            continue
        if suspicious_supplier_name(ln) or bad_supplier_line(ln):
            continue
        if re.search(r"\b(invoice|date|vat|tax|customer|bill to|ship to|total|amount due|subtotal|net total|total net|gross|bcrs|deposit|amount)\b", ln, re.I):
            continue
        if re.search(r"[A-Za-z]", ln) and len(ln) >= 4 and len(ln) <= 80:
            if _company_strength_score(ln) >= 6:
                candidates.append(ln)
    deduped = _dedupe_candidates(candidates)
    if primary:
        pkey = " ".join(str(primary).split()).strip().lower()
        rest = [c for c in deduped if c.lower() != pkey]
        rest = sorted(rest, key=_company_strength_score, reverse=True)
        return [primary] + rest
    return sorted(deduped, key=_company_strength_score, reverse=True)


def _collect_invoice_number_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    primary = first_match([
        # Label-only layouts: "Invoice 10512630".  Exclude "Invoice To" and
        # date/total labels so phone/VAT numbers are not promoted.
        r"\binvoice\s+(?!to\b|date\b|total\b|amount\b)([A-Z0-9][A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
        r"invoice\s*(?:no\.?|number|#|nr\.?)\s*[.:\-]*\s*([A-Z0-9][A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
        r"invoice\s*(?:no\.?|number|#|nr\.?)\s*[.:\-]*\s*([0-9][A-Z0-9\/\-_]*)",
        r"\bINV[.\-_]?([0-9][A-Z0-9\/\-_]*)",
        r"\bdocument\s*(?:no\.?|number|nr\.?)\s*[.:\-]*\s*([A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
        r"\binv(?:oice|oiice|oice)?\s*[.:\-]+\s*([A-Z0-9\/\-_]*[0-9][A-Z0-9\/\-_]*)",
    ], text)
    if primary and not suspicious_invoice_number(primary):
        candidates.append(primary)
    fallback = _invoice_number_fallback(text)
    if fallback and not suspicious_invoice_number(fallback):
        candidates.append(fallback)
    top_lines = text.splitlines()[:30]
    for line in top_lines:
        if re.search(r"\b(tel|fax|phone|mob|mobile|vat|tax|bcrs|iban|swift)\b|^\s*[tmfw]\s*[:\-]|\+\s*\d", line, re.I):
            continue
        for m in re.finditer(r"\b([A-Z]{1,4}[\-\/]?[0-9]{3,}|[0-9]{4,}[A-Z0-9\-/]*)\b", line):
            cand = m.group(1)
            if not suspicious_invoice_number(cand):
                candidates.append(cand)
    return _dedupe_candidates(candidates)


def _invoice_number_fallback(text: str) -> str | None:
    """Fallback invoice-number extractor for invoice layouts where the number
    appears in the top header region but is not reliably captured by standard patterns.

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
    header_text = _header_region_text(text, max_lines=36)
    supplier_header_text = _header_region_text(text, max_lines=18)
    totals_text = _totals_region_text(text, tail_lines=28)
    identity_text = header_text or clean_text(text)

    invoice_candidates = _collect_invoice_number_candidates(identity_text)  # includes _invoice_number_fallback replay
    invoice_number = _choose_best_invoice_candidate(invoice_candidates, text=identity_text)
    if not invoice_number:
        invoice_number = _choose_best_invoice_candidate(
            [_invoice_number_fallback(identity_text)],
            text=identity_text,
        )

    invoice_date_raw = _extract_invoice_date_value(identity_text)
    if not invoice_date_raw:
        invoice_date_raw = _extract_invoice_date_value(text)
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
    _deposit_candidate = None

    # Finance-labelled line pass: fixes OCR/DI text such as
    #   VAT 18% 525.18  -> VAT amount 525.18, not rate 18
    #   TOTAL 3,442.85  -> total, not SUBTOTAL
    # It is reconciliation-aware and only overrides weak/generic regex values.
    _labelled_bundle = _extract_labeled_financial_bundle(text)
    if _labelled_bundle:
        _tmp_amounts = {
            "net_amount": net_amount,
            "vat_amount": vat_amount,
            "total_amount": total_amount,
        }
        _tmp_amounts = _apply_financial_remediation(
            _tmp_amounts, text, "labelled_summary"
        )
        net_amount = _tmp_amounts.get("net_amount")
        vat_amount = _tmp_amounts.get("vat_amount")
        total_amount = _tmp_amounts.get("total_amount")
        _deposit_candidate = _labelled_bundle.get("_deposit_candidate")

    supplier_candidates = _dedupe_candidates(
        _collect_supplier_candidates(supplier_header_text or identity_text, account_tokens=account_tokens)
    )
    supplier_name = supplier_candidates[0] if supplier_candidates else None
    supplier_vat = _extract_supplier_vat_number(supplier_header_text or identity_text) or _extract_supplier_vat_number(text)
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
        if _struct.get("_deposit_candidate") is not None:
            _deposit_candidate = _struct["_deposit_candidate"]
        # _deposit_candidate is passed through the return dict for BCRS detection
        # in batches.py; it does NOT by itself trigger a split.

    _field_sources: dict[str, str] = {}
    if len(invoice_candidates) > 1:
        ranked = _rank_candidates_with_llm("invoice_number", invoice_candidates, text, openai_api_key, model=settings.openai_model)
        if ranked and ranked.get("chosen_candidate") and ranked.get("confidence") in ("high", "medium", "strong"):
            invoice_number = ranked["chosen_candidate"]
            _field_sources["invoice_number"] = "llm_ranking"
        elif ranked and ranked.get("review_recommended"):
            _field_sources["invoice_number"] = "llm_review"
    if len(supplier_candidates) > 1:
        ranked = _rank_candidates_with_llm("supplier_name", supplier_candidates, text, openai_api_key, model=settings.openai_model)
        if ranked and ranked.get("chosen_candidate") and ranked.get("confidence") in ("high", "medium", "strong"):
            supplier_name = ranked["chosen_candidate"]
            _field_sources["supplier_name"] = "llm_ranking"
        elif ranked and ranked.get("review_recommended"):
            _field_sources["supplier_name"] = "llm_review"
    resolved_currency = _extract_currency_code(text)
    if supplier_name and "supplier_name" not in _field_sources:
        _field_sources["supplier_name"] = "header_supplier"
    if invoice_number and "invoice_number" not in _field_sources:
        _field_sources["invoice_number"] = "header_identity"
    if invoice_date and "invoice_date" not in _field_sources:
        _field_sources["invoice_date"] = "header_identity"
    if resolved_currency and "currency" not in _field_sources:
        _field_sources["currency"] = "text_rules"

    return {
        "supplier_name": supplier_name,
        "supplier_vat": supplier_vat,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "description": description,
        "line_items_raw": line_items_raw,
        "net_amount": net_amount,
        "vat_amount": vat_amount,
        "total_amount": total_amount,
        "currency": resolved_currency,
        "tax_code": None,
        "_deposit_candidate": _deposit_candidate,
        "_field_sources": _field_sources,
        "_invoice_candidates": invoice_candidates,
        "_supplier_candidates": supplier_candidates,
        "_header_text": header_text,
        "_supplier_header_text": supplier_header_text,
        "_totals_text": totals_text,
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
_ocr_fallback_error: str | None = None


def _reset_azure_di_error() -> None:
    """Clear the Azure DI circuit-breaker flag.

    Called by the batch job when the preflight check has already determined
    that Azure DI should not be used for this batch — ensures that any
    leftover error state from a previous batch doesn't persist across runs.
    Also useful in tests to reset module-level state between cases.
    """
    global _azure_di_error
    _azure_di_error = None


def _reset_ocr_fallback_error() -> None:
    """Clear the OCR fallback circuit-breaker for a new batch."""
    global _ocr_fallback_error
    _ocr_fallback_error = None


def _ocr_error_is_batch_terminal(error: Exception) -> bool:
    text = str(error or "").lower()
    return any(token in text for token in (
        "quota", "rate limit", "ratelimit", "too many requests", "exceeded",
        "maximum", "limit reached", "insufficient", "subscription",
    ))


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


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _serialise_di_field(field: Any) -> dict[str, Any] | None:
    if field is None:
        return None
    payload: dict[str, Any] = {
        "content": _json_safe(getattr(field, "content", None) or (field.get("content") if isinstance(field, dict) else None)),
        "confidence": _json_safe(getattr(field, "confidence", None) or (field.get("confidence") if isinstance(field, dict) else None)),
        "value_type": _json_safe(getattr(field, "value_type", None) or (field.get("valueType") if isinstance(field, dict) else None)),
    }
    for attr in ("value_string", "value_number", "value_date", "value_phone_number"):
        value = getattr(field, attr, None)
        if value is not None:
            payload[attr] = _json_safe(value)
    try:
        if getattr(field, "value_currency", None) is not None:
            payload["value_currency"] = {
                "amount": _json_safe(getattr(field.value_currency, "amount", None)),
                "currency_code": _json_safe(getattr(field.value_currency, "currency_code", None)),
                "currency_symbol": _json_safe(getattr(field.value_currency, "currency_symbol", None)),
            }
    except Exception:
        pass
    try:
        if getattr(field, "value_address", None) is not None:
            payload["value_address"] = {
                "road": _json_safe(getattr(field.value_address, "road", None)),
                "city": _json_safe(getattr(field.value_address, "city", None)),
                "state": _json_safe(getattr(field.value_address, "state", None)),
                "postal_code": _json_safe(getattr(field.value_address, "postal_code", None)),
                "country_region": _json_safe(getattr(field.value_address, "country_region", None)),
            }
    except Exception:
        pass
    try:
        value_array = getattr(field, "value_array", None)
    except Exception:
        value_array = None
    if value_array is None and isinstance(field, dict):
        value_array = field.get("valueArray") or field.get("value_array")
    if value_array is not None:
        payload["value_array"] = [_serialise_di_field(item) for item in value_array]
    try:
        value_object = getattr(field, "value_object", None)
    except Exception:
        value_object = None
    if value_object is None and isinstance(field, dict):
        value_object = field.get("valueObject") or field.get("value_object")
    if value_object is not None:
        payload["value_object"] = {
            str(name): _serialise_di_field(item)
            for name, item in value_object.items()
        }
    return payload


def _serialise_di_document(result: Any) -> dict[str, Any]:
    documents = getattr(result, "documents", None) or []
    document = documents[0] if documents else None
    fields = getattr(document, "fields", None) or {}
    raw_fields = {str(name): _serialise_di_field(field) for name, field in fields.items()}
    pages = []
    for page in (getattr(result, "pages", None) or []):
        pages.append({
            "page_number": _json_safe(getattr(page, "page_number", None)),
            "angle": _json_safe(getattr(page, "angle", None)),
            "width": _json_safe(getattr(page, "width", None)),
            "height": _json_safe(getattr(page, "height", None)),
            "unit": _json_safe(getattr(page, "unit", None)),
        })
    return {
        "model_id": _json_safe(getattr(result, "model_id", None)),
        "content": _json_safe(getattr(result, "content", None)),
        "pages": pages,
        "document_count": len(documents),
        "document": {
            "doc_type": _json_safe(getattr(document, "doc_type", None)) if document is not None else None,
            "confidence": _json_safe(getattr(document, "confidence", None)) if document is not None else None,
            "fields": raw_fields,
        },
    }


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
            polling_interval=1,
            connection_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 45)), 20.0),
            read_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 45)), 20.0),
        )
        # ── Per-page timeout safeguard ─────────────────────────────────────
        # poller.result() can block indefinitely if Azure DI hangs.  We run
        # it in a daemon thread so we can enforce a wall-clock timeout and
        # fall back cleanly rather than leaving the batch stuck at 0 %.
        # Default: 45 s per page (configurable via AZURE_DI_PAGE_TIMEOUT_S).
        import concurrent.futures as _cf
        _page_timeout = float(getattr(settings, "azure_di_page_timeout_s", 45))
        _pool = _cf.ThreadPoolExecutor(max_workers=1)
        _future = _pool.submit(poller.result)
        try:
            result = _future.result(timeout=_page_timeout)
        except _cf.TimeoutError:
                logger.warning(
                    "Azure DI page timeout after %.0fs; "
                    "using fallback for this page; DI remains enabled for later pages",
                    _page_timeout,
                )
                _future.cancel()
                _pool.shutdown(wait=False, cancel_futures=True)
                return None
        finally:
            if _future.done():
                _pool.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        exc_str = str(exc)
        # Classify the real failure and open the circuit-breaker for permanent errors.
        # Map HTTP status codes / exception text to human-readable causes.
        if any(token in exc_str for token in ("401", "Unauthorized")):
            cause = "authentication failed (HTTP 401) — check AZURE_DI_KEY"
            is_permanent = True
        elif any(token in exc_str for token in ("403", "Forbidden", "VirtualNetwork", "AuthorizationFailed")):
            cause = "authorisation denied (HTTP 403) — check AZURE_DI_KEY, VNet rules, or quota"
            is_permanent = True
        elif "404" in exc_str:
            cause = "endpoint or model not found (HTTP 404) — check AZURE_DI_ENDPOINT"
            is_permanent = True
        elif any(token in exc_str.lower() for token in ("429", "quota", "rate limit", "ratelimit", "too many requests", "exceeded")):
            cause = "throttled / rate-limited (HTTP 429) — retry later or reduce concurrency"
            is_permanent = True
        elif any(token in exc_str for token in ("500", "502", "503", "504")):
            cause = f"Azure service-side error — {exc_str[:120]}"
            is_permanent = False
        else:
            cause = exc_str[:200]
            is_permanent = False

        if is_permanent:
            _azure_di_error = cause
            logger.error(
                "Azure DI extraction FAILED (permanent) — circuit breaker opened. "
                "Cause: %s. All remaining pages will fall back to OpenAI vision.",
                cause,
            )
        else:
            logger.warning("Azure DI extraction failed (transient): %s", cause)
        return None

    if not result.documents:
        logger.info("Azure DI returned no documents")
        return None

    document = result.documents[0]
    fields = document.fields or {}

    # ── Core fields ────────────────────────────────────────────────────────
    supplier_name, s_conf     = _str(fields.get("VendorName"))
    supplier_name = _clean_di_supplier_name(_di_field_content_text(fields.get("VendorName")) or supplier_name)
    supplier_addr, _          = _str(fields.get("VendorAddress"))
    if not supplier_addr:
        supplier_addr = _addr(fields.get("VendorAddress"))
    supplier_vat, _           = _str(fields.get("VendorTaxId"))
    supplier_addr_recipient, _ = _str(fields.get("VendorAddressRecipient"))

    customer_name, c_conf     = _str(fields.get("CustomerName"))
    customer_addr, _          = _str(fields.get("CustomerAddress"))
    if not customer_addr:
        customer_addr = _addr(fields.get("CustomerAddress"))
    customer_vat, _           = _str(fields.get("CustomerTaxId"))
    customer_addr_recipient, _ = _str(fields.get("CustomerAddressRecipient"))

    invoice_number, _         = _str(fields.get("InvoiceId"))
    invoice_date, _           = _date(fields.get("InvoiceDate"))
    due_date, _               = _date(fields.get("DueDate"))
    order_number, _           = _str(fields.get("OrderNumber"))
    purchase_order, _         = _str(fields.get("PurchaseOrder"))

    net_amount, t_conf_sub    = _num(fields.get("SubTotal"))
    vat_amount, t_conf_tax    = _num(fields.get("TotalTax"))
    total_amount, t_conf_tot  = _num(fields.get("InvoiceTotal"))
    if total_amount is None:
        total_amount, _       = _num(fields.get("AmountDue"))
    if total_amount is None and net_amount is not None and vat_amount is not None:
        total_amount = round(float(net_amount) + float(vat_amount), 2)
        t_conf_tot = round(max(t_conf_sub, t_conf_tax) * 0.95, 2)

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
    # Azure DI's semantic invoice fields and full-page OCR text come from the
    # same provider response.  Use the text as a zero-extra-call recovery layer
    # for blank dates and missing/mismatched totals before trying heavier
    # providers.
    di_page_text_for_recovery = getattr(result, "content", None) or ""
    if invoice_date is None and di_page_text_for_recovery:
        recovered_date = parse_date(_extract_invoice_date_value(di_page_text_for_recovery))
        if recovered_date is not None:
            invoice_date = recovered_date

    if di_page_text_for_recovery:
        recovered_amounts = _apply_financial_remediation(
            {
                "net_amount": net_amount,
                "vat_amount": vat_amount,
                "total_amount": total_amount,
                "method_used": "azure_di",
            },
            di_page_text_for_recovery,
            "azure_di_content_recovery",
        )
        if recovered_amounts.get("net_amount") is not None and recovered_amounts.get("net_amount") != net_amount:
            net_amount = recovered_amounts.get("net_amount")
            t_conf_sub = max(t_conf_sub, 0.65)
        if recovered_amounts.get("vat_amount") is not None and recovered_amounts.get("vat_amount") != vat_amount:
            vat_amount = recovered_amounts.get("vat_amount")
            t_conf_tax = max(t_conf_tax, 0.65)
        if recovered_amounts.get("total_amount") is not None and recovered_amounts.get("total_amount") != total_amount:
            total_amount = recovered_amounts.get("total_amount")
            t_conf_tot = max(t_conf_tot, 0.65)

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
    di_raw_payload = _serialise_di_document(result)
    di_raw_fields = (di_raw_payload.get("document") or {}).get("fields") or {}

    return {
        "extraction_source": "azure_di",
        "supplier_name":    supplier_name,
        "supplier_address": supplier_addr,
        "supplier_address_recipient": supplier_addr_recipient,
        "supplier_vat":     supplier_vat,
        "customer_name":    customer_name,
        "customer_address": customer_addr,
        "customer_address_recipient": customer_addr_recipient,
        "customer_vat":     customer_vat,
        "invoice_number":   invoice_number,
        "invoice_date":     invoice_date,
        "due_date":         due_date,
        "order_number":     order_number,
        "purchase_order":   purchase_order,
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
        "_di_structured_fields": {
            "supplier_name": supplier_name,
            "supplier_vat": supplier_vat,
            "supplier_address": supplier_addr,
            "supplier_address_recipient": supplier_addr_recipient,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "order_number": order_number,
            "purchase_order": purchase_order,
            "net_amount": net_amount,
            "vat_amount": vat_amount,
            "total_amount": total_amount,
            "currency": currency,
            "customer_name": customer_name,
            "customer_address": customer_addr,
            "customer_address_recipient": customer_addr_recipient,
            "customer_vat": customer_vat,
        },
        "_di_raw_fields": di_raw_fields,
        "_di_raw_payload": di_raw_payload,
        "raw_di_document_confidence": getattr(document, "confidence", None),
        "ai_confidence": {
            "supplier": round(s_conf, 2),
            "customer": round(c_conf, 2),
            "lines":    round(items_conf, 2),
            "totals":   round(totals_conf, 2),
        },
    }


def azure_di_extract_read_text(
    jpeg_bytes: bytes,
    endpoint: str,
    key: str,
) -> dict[str, Any] | None:
    """Read page OCR text with Azure DI when invoice extraction has no fields."""
    if not jpeg_bytes or not endpoint or not key:
        return None
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential
        import concurrent.futures as _cf

        client = DocumentIntelligenceClient(
            endpoint=endpoint.rstrip("/"),
            credential=AzureKeyCredential(key),
        )
        poller = client.begin_analyze_document(
            "prebuilt-read",
            body=jpeg_bytes,
            content_type="image/jpeg",
            polling_interval=1,
            connection_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 45)), 20.0),
            read_timeout=min(float(getattr(settings, "azure_di_page_timeout_s", 45)), 20.0),
        )
        timeout_s = float(getattr(settings, "azure_di_page_timeout_s", 45))
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(poller.result)
        try:
            result = future.result(timeout=timeout_s)
        except _cf.TimeoutError:
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            logger.warning("Azure DI read fallback timeout after %.0fs", timeout_s)
            return None
        finally:
            if future.done():
                pool.shutdown(wait=False, cancel_futures=True)
        content = clean_text(getattr(result, "content", None) or "")
        if count_meaningful_chars(content) < 10:
            return None
        return {
            "extraction_source": "azure_di_read",
            "document_type": "read",
            "di_page_text": content,
            "_di_raw_fields": {},
            "_di_raw_payload": _serialise_di_document(result),
        }
    except Exception as exc:
        logger.warning("Azure DI read fallback failed: %s", exc)
        return None


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
    field_sources = dict(merged.get("_field_sources") or {})

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

    is_azure_di = ai.get("extraction_source") == "azure_di"
    resolved_supplier, resolved_source, supplier_meta = _resolve_supplier_identity(
        merged,
        ai,
        account_company_name=account_company_name,
    )
    if resolved_supplier:
        merged["supplier_name"] = resolved_supplier
        field_sources["supplier_name"] = resolved_source or (
            "azure_di_structured" if is_azure_di else "header_supplier"
        )
        merged["_supplier_resolution"] = supplier_meta
    else:
        merged["supplier_name"] = None
        merged["_supplier_resolution"] = supplier_meta

    # -- Invoice number --------------------------------------------------------
    ai_invoice_number = ai.get("invoice_number")
    if ai_invoice_number:
        current_invoice = merged.get("invoice_number")
        current_score = _invoice_candidate_quality_score(current_invoice, text=merged.get("di_page_text") or base.get("di_page_text") or "")
        ai_score = _invoice_candidate_quality_score(ai_invoice_number, text=ai.get("di_page_text") or "")
        if current_score < 0 or ai_score >= current_score + 3:
            merged["invoice_number"] = ai_invoice_number
            field_sources["invoice_number"] = "azure_di_structured" if is_azure_di else "openai_ai"

    # -- Date fields -----------------------------------------------------------
    if merged.get("invoice_date") is None and ai.get("invoice_date") is not None:
        merged["invoice_date"] = ai.get("invoice_date")
        field_sources["invoice_date"] = "azure_di_structured" if is_azure_di else "openai_ai"
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
        base_support = _amount_support_score(
            merged.get("net_amount"),
            merged.get("vat_amount"),
            merged.get("total_amount"),
            merged.get("_deposit_candidate"),
        )
        ai_support = _amount_support_score(
            ai.get("net_amount"),
            ai.get("vat_amount"),
            ai.get("total_amount"),
            ai.get("_deposit_candidate") if isinstance(ai, dict) else None,
        )
        for field in ("net_amount", "vat_amount", "total_amount"):
            ai_val = ai.get(field)
            base_val = merged.get(field)
            if ai_val is None:
                continue
            if _prefer_base_amount(base_val, ai_val) and base_support >= ai_support:
                continue
            if base_support >= ai_support + 4 and base_val is not None:
                continue
            # Preserve structured values that only reconcile when a known deposit/BCRS
            # candidate is included. This prevents Azure DI from zeroing VAT on invoices
            # where total = net + VAT + deposit.
            if (
                field == "vat_amount"
                and base_val is not None
                and merged.get("_deposit_candidate") not in (None, "")
                and base_support >= ai_support
            ):
                continue
            merged[field] = ai_val
            field_sources[field] = "azure_di_structured"
    else:
        if merged.get("net_amount") is None and ai.get("net_amount") is not None:
            merged["net_amount"] = ai.get("net_amount")
            field_sources["net_amount"] = "openai_ai"
        if merged.get("vat_amount") is None and ai.get("vat_amount") is not None:
            merged["vat_amount"] = ai.get("vat_amount")
            field_sources["vat_amount"] = "openai_ai"
        if merged.get("total_amount") is None and ai.get("total_amount") is not None:
            merged["total_amount"] = ai.get("total_amount")
            field_sources["total_amount"] = "openai_ai"

    # -- Metadata --------------------------------------------------------------
    if not merged.get("currency") and ai.get("currency"):
        merged["currency"] = ai.get("currency")
        field_sources["currency"] = "azure_di_structured" if is_azure_di else "openai_ai"
    if not merged.get("tax_code") and ai.get("tax_code"):
        merged["tax_code"] = ai.get("tax_code")
    if merged.get("description") in (None, "", "Invoice extraction", "Invoice goods or services") and ai.get("description"):
        merged["description"] = ai.get("description")
        field_sources["description"] = "azure_di_structured" if is_azure_di else "openai_ai"

    # -- Extended framework fields (not in rule-based base) --------------------
    for field in (
        "supplier_address", "supplier_address_recipient", "supplier_vat", "supplier_email", "supplier_phone",
        "customer_name", "customer_address", "customer_address_recipient", "customer_vat",
        "order_number", "purchase_order",
        "document_type", "extraction_status", "totals_reconcile",
        "ai_issues", "ai_confidence",
        "line_items_structured",   # structured list from Azure DI / OpenAI
        "extraction_source",       # tracks which engine produced the result
        "di_page_text",            # full OCR text from Azure DI (used by BCRS detection)
        "_di_structured_fields",   # raw structured Azure DI field values before merge
        "_di_raw_fields",          # exact serialised field payload returned by Azure DI
        "_di_raw_payload",         # serialised top-level DI document payload
    ):
        if ai.get(field) is not None:
            merged[field] = ai[field]

    if field_sources:
        merged["_field_sources"] = field_sources

    return merged


def _build_provider_baseline_result(
    extracted: dict[str, Any],
    *,
    method: str,
    page_index: int,
    page_quality_score: float,
    page_text: str,
) -> dict[str, Any]:
    out = dict(extracted or {})
    source = str(out.get("extraction_source") or ("azure_di" if "azure_di" in method else method)).strip() or "raw_extraction"
    cleaned_text = clean_text(page_text or out.get("di_page_text") or "")
    header_text = out.get("_header_text") or _header_region_text(cleaned_text, max_lines=24)
    totals_text = out.get("_totals_text") or _totals_region_text(cleaned_text, tail_lines=24)
    raw_di_fields = out.get("_di_raw_fields") or {}
    raw_vendor_name = _di_field_content_text(raw_di_fields.get("VendorName")) if isinstance(raw_di_fields, dict) else None
    out["supplier_name"] = raw_vendor_name or _collapse_ws(out.get("supplier_name"))
    if out.get("invoice_date") is not None:
        out["invoice_date"] = parse_date(out.get("invoice_date"))
    if out.get("due_date") is not None:
        out["due_date"] = parse_date(out.get("due_date"))
    for field in ("net_amount", "vat_amount", "total_amount"):
        if out.get(field) is not None:
            try:
                out[field] = round(float(out.get(field)), 2)
            except Exception:
                pass
    if not out.get("currency"):
        out["currency"] = (
            _extract_currency_code(header_text)
            or _extract_currency_code(totals_text)
            or _extract_currency_code(cleaned_text)
        )
    ai_conf = out.get("ai_confidence") or {}
    if ai_conf:
        supplier_conf = float(ai_conf.get("supplier", 0.0) or 0.0)
        totals_conf = float(ai_conf.get("totals", 0.0) or 0.0)
        confidence = round(min(max((supplier_conf * 0.45) + (totals_conf * 0.45) + 0.10, 0.0), 0.99), 2)
    else:
        completeness = sum(
            1
            for value in (
                out.get("supplier_name"),
                out.get("invoice_number"),
                out.get("invoice_date"),
                out.get("total_amount"),
            )
            if value not in (None, "", [])
        )
        confidence = round(min(0.35 + (completeness * 0.15), 0.9), 2)
    missing_core = any(
        out.get(field) in (None, "")
        for field in ("supplier_name", "invoice_number", "invoice_date", "total_amount")
    )
    out.update({
        "page_no": page_index + 1,
        "method_used": f"{method}+provider_baseline",
        "extraction_source": source,
        "confidence_score": confidence,
        "validation_status": "review_provider_baseline" if missing_core else "ok_provider_baseline",
        "review_required": bool(missing_core),
        "review_priority": "medium" if missing_core else None,
        "review_reasons": "provider_baseline_missing_core" if missing_core else None,
        "review_fields": "supplier_name|invoice_number|invoice_date|total_amount" if missing_core else None,
        "auto_approved": False if missing_core else True,
        "page_quality_score": round(page_quality_score, 2),
        "supplier_match_method": "provider_direct",
        "header_raw": header_text,
        "totals_raw": totals_text,
        "page_text_raw": cleaned_text[:20000],
    })
    return out


def process_pdf_page(
    pdf_path: str | Path,
    page_index: int,
    openai_api_key: str | None = None,
    account_company_name: str | None = None,
    skip_azure_di: bool = False,
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
    logger.info("process_pdf_page build=%s page=%d file=%s", EXTRACTOR_BUILD_TAG, page_index, Path(pdf_path).name)
    pdf_path = Path(pdf_path)

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 1 — Document acquisition, preprocessing & quality assessment
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 1: acquiring page %d from %s", page_index, pdf_path.name)

    # Preferred provider order:
    #   1. Azure Document Intelligence structured extraction, when configured
    #      and not circuit-broken by a prior fast failure.
    #   2. Native PDF text, only when it is useful enough to parse safely.
    #   3. OCR.space/local OCR fallback.
    #   4. Targeted OCR/region crop reads later during saved-region replay.
    native_text = extract_native_pdf_page(pdf_path, page_index)
    method = "provider_start"
    page_quality_score: float = 0.5  # default until we render the image

    provider_baseline_mode = bool(getattr(settings, "scan_provider_baseline_mode", False))
    use_vision = bool(settings.use_openai and openai_api_key and not provider_baseline_mode)
    final_text = "(page text unavailable)"
    ai_fields = None
    di_read_payload = None
    raw_jpeg = None
    jpeg_bytes = None

    _di_ok, _di_reason = azure_di_available()
    use_azure_di = _di_ok and not skip_azure_di
    use_azure_di_read_fallback = bool(
        getattr(settings, "azure_di_read_text_fallback", False)
        and not skip_azure_di
        and
        settings.use_azure_di
        and settings.azure_di_endpoint
        and settings.azure_di_key
        and (
            _di_ok
            or ("timeout" in str(_di_reason or "").lower())
            or ("timed out" in str(_di_reason or "").lower())
        )
    )
    if not _di_ok and settings.use_azure_di:
        logger.debug("Azure DI skipped: %s", _di_reason)

    if use_azure_di or use_azure_di_read_fallback or use_vision:
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

        if raw_jpeg:
            processed_jpeg, page_quality_score = preprocess_page_image(raw_jpeg)
            jpeg_bytes = raw_jpeg if page_quality_score >= 0.62 else processed_jpeg
            logger.debug("Page %d quality score: %.2f (image_source=%s)", page_index, page_quality_score, "raw" if jpeg_bytes is raw_jpeg else "processed")

    # 1. Azure DI before OCR where safe. The availability check is config/circuit
    # based and the extraction call has its own short page timeout; any failure
    # falls through to native text/OCR without logging secrets or invoice text.
    if use_azure_di and jpeg_bytes:
        ai_fields = azure_di_extract_invoice(
            jpeg_bytes,
            settings.azure_di_endpoint,
            settings.azure_di_key,
        )
        if ai_fields:
            method = "azure_di"
            _di_text = ai_fields.get("di_page_text") or ""
            if count_meaningful_chars(_di_text) >= 10:
                final_text = _di_text
            logger.info("Azure DI extraction succeeded for page %d", page_index)

    # If the invoice model fails or returns too little text, use Azure DI Read
    # OCR on the same upright rendered page before falling back to native text.
    if use_azure_di_read_fallback and jpeg_bytes and (
        ai_fields is None
        or count_meaningful_chars((ai_fields or {}).get("di_page_text") or "") < 20
        or not _extraction_has_minimum_invoice_fields(ai_fields)
    ):
        di_read_payload = azure_di_extract_read_text(
            jpeg_bytes,
            settings.azure_di_endpoint,
            settings.azure_di_key,
        )
        _read_text = (di_read_payload or {}).get("di_page_text") or ""
        if count_meaningful_chars(_read_text) >= 10:
            if final_text == "(page text unavailable)" or count_meaningful_chars(final_text) < count_meaningful_chars(_read_text):
                final_text = _read_text
            if ai_fields is None:
                method = "azure_di_read"
            logger.info("Azure DI Read fallback supplied OCR text for page %d", page_index)

    if provider_baseline_mode:
        # Provider-baseline mode: DI first, immediate OCR fallback, native text last.
        if final_text == "(page text unavailable)":
            ocr_text, ocr_method = _get_fallback_ocr_text(pdf_path, page_index, native_text)
            if ocr_text and count_meaningful_chars(ocr_text) >= 10:
                final_text = ocr_text
                method = ocr_method or "ocr_primary"
        if final_text == "(page text unavailable)" and _native_text_looks_usable(native_text):
            final_text = native_text
            method = "native_text"
        if final_text == "(page text unavailable)":
            method = "provider_unavailable"
    else:
        # 2. Native text is useful for digital PDFs, but only if it carries invoice
        # signals. It remains a candidate source rather than a blind replacement.
        if final_text == "(page text unavailable)" and _native_text_looks_usable(native_text):
            final_text = native_text
            method = "native_text"

        # 3. OCR fallback remains available for OCR-only deployments and for pages
        # where DI/native text is missing or weak.
        if final_text == "(page text unavailable)":
            ocr_text, ocr_method = _get_fallback_ocr_text(pdf_path, page_index, native_text)
            if ocr_text and count_meaningful_chars(ocr_text) >= 10:
                final_text = ocr_text
                method = ocr_method or "ocr_primary"
            else:
                method = "ocr_unavailable_native_text_ignored"

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 2 — Field extraction
    # ─────────────────────────────────────────────────────────────────────────
    logger.debug("Stage 2: field extraction for page %d", page_index)

    if provider_baseline_mode:
        if ai_fields and di_read_payload and not _extraction_has_minimum_invoice_fields(ai_fields):
            fallback_extracted = simple_extract(
                di_read_payload.get("di_page_text") or "",
                openai_api_key=None,
                account_company_name=account_company_name,
            )
            changed = False
            if _fallback_extraction_is_usable(fallback_extracted):
                for field in (
                    "supplier_name", "supplier_vat", "invoice_number", "invoice_date",
                    "description", "net_amount", "vat_amount", "total_amount", "currency",
                ):
                    if ai_fields.get(field) in (None, "", []) and fallback_extracted.get(field) not in (None, "", []):
                        ai_fields[field] = fallback_extracted[field]
                        changed = True
            if changed:
                ai_fields["extraction_source"] = "azure_di+azure_di_read_text_fallback"
                ai_fields["di_page_text"] = di_read_payload.get("di_page_text") or ai_fields.get("di_page_text") or ""
                ai_fields["_di_read_raw_payload"] = di_read_payload.get("_di_raw_payload")
                method = f"{method}+di_read_text_fallback"
        if ai_fields:
            return _build_provider_baseline_result(
                ai_fields,
                method=method,
                page_index=page_index,
                page_quality_score=page_quality_score,
                page_text=ai_fields.get("di_page_text") or final_text,
            )
        if di_read_payload:
            extracted = simple_extract(
                di_read_payload.get("di_page_text") or final_text,
                openai_api_key=None,
                account_company_name=account_company_name,
            )
            if not _fallback_extraction_is_usable(extracted):
                extracted = {
                    "extraction_source": "azure_di_read_unusable",
                    "di_page_text": di_read_payload.get("di_page_text") or "",
                    "_di_raw_fields": {},
                    "_di_raw_payload": di_read_payload.get("_di_raw_payload"),
                }
                return _build_provider_baseline_result(
                    extracted,
                    method="azure_di_read_unusable",
                    page_index=page_index,
                    page_quality_score=page_quality_score,
                    page_text=di_read_payload.get("di_page_text") or final_text,
                )
            extracted["extraction_source"] = "azure_di_read"
            extracted["di_page_text"] = di_read_payload.get("di_page_text") or ""
            extracted["_di_raw_fields"] = {}
            extracted["_di_raw_payload"] = di_read_payload.get("_di_raw_payload")
            return _build_provider_baseline_result(
                extracted,
                method="azure_di_read",
                page_index=page_index,
                page_quality_score=page_quality_score,
                page_text=di_read_payload.get("di_page_text") or final_text,
            )
        extracted = simple_extract(
            final_text,
            openai_api_key=None,
            account_company_name=account_company_name,
        )
        return _build_provider_baseline_result(
            extracted,
            method=method,
            page_index=page_index,
            page_quality_score=page_quality_score,
            page_text=final_text,
        )

    # 2a — Rule-based baseline (no API cost, instant)
    extracted = simple_extract(
        final_text,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
    )

    # 2b - Azure Document Intelligence remediation over the structured
    # candidate already read before OCR/native fallback.
    if ai_fields:
        extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
        _di_text = ai_fields.get("di_page_text") or ""
        if count_meaningful_chars(_di_text) >= 20:
            try:
                _recovery = simple_extract(
                    _di_text,
                    openai_api_key=None,
                    account_company_name=account_company_name,
                )
                extracted = _merge_text_recovery_fields(
                    extracted, _recovery, "di_text_recovery"
                )
                extracted = _apply_financial_remediation(
                    extracted, _di_text, "di_text_reconciliation"
                )
            except Exception as _rec_exc:
                logger.debug("DI text remediation failed p%d: %s", page_index, _rec_exc)

    # 2c - OpenAI vision fallback when Azure DI unavailable or returned nothing.
    if ai_fields is None and use_vision and jpeg_bytes:
        jpeg_b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        ai_fields = openai_extract_invoice_vision(
            jpeg_b64,
            final_text,
            openai_api_key,
            model=settings.openai_model,
            account_company_name=account_company_name,
        )
        if ai_fields:
            extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
            method = f"{method}+vision"

    # 2d - Text-only AI fallback when image extraction produced nothing.
    if ai_fields is None and use_vision:
        logger.info("Image unavailable p%d - text-only AI fallback", page_index)
        _text_for_ai = final_text if count_meaningful_chars(final_text) >= 20 else ""
        if count_meaningful_chars(_text_for_ai) >= 20:
            ai_fields = openai_extract_invoice_fields(
                _text_for_ai, openai_api_key,
                model=settings.openai_model,
                account_company_name=account_company_name,
            )
            if ai_fields:
                extracted = merge_ai_fields(extracted, ai_fields, account_company_name)
                method = f"{method}+openai_text"

    # 2e - OpenAI validation pass (cross-checks the merged result).
    if use_vision and openai_api_key:
        validation_result = openai_validate_extraction(
            final_text,
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
    if not extracted.get("currency"):
        resolved_currency = (
            _extract_currency_code(extracted.get("_header_text"))
            or _extract_currency_code(extracted.get("_totals_text"))
            or _extract_currency_code(final_text)
        )
        if resolved_currency:
            extracted["currency"] = resolved_currency
    logger.debug("Stage 3: line normalization for page %d", page_index)

    # 3a0 — deterministic financial remediation over the best available text.
    # This runs after all extraction providers so it can correct missed/misread
    # DI numbers before formal validation and review-flagging.
    try:
        _remediation_text = "\n".join(
            part for part in (final_text, extracted.get("di_page_text") or "")
            if part and count_meaningful_chars(str(part)) >= 10
        )
        if _remediation_text:
            extracted = _apply_financial_remediation(
                extracted, _remediation_text, "financial_remediation"
            )
    except Exception as _fre:
        logger.debug("financial remediation failed p%d: %s", page_index, _fre)

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
            supplier_resolution = extracted.get("_supplier_resolution") or {}
            supplier_source = str((extracted.get("_field_sources") or {}).get("supplier_name") or "")
            _snorm = _normalize_supplier(clean_supplier, supplier_vat=supplier_vat_s3)
            if _snorm.match_method == "unmatched" and _document_supplier_evidence_is_strong(
                clean_supplier,
                supplier_vat=supplier_vat_s3,
                ai_confidence=extracted.get("ai_confidence") or {},
            ):
                _snorm.match_method = "document_header_vat" if supplier_vat_s3 else "document_header"
                _snorm.match_confidence = max(_snorm.match_confidence, 0.82 if supplier_vat_s3 else 0.74)
                _snorm.review_reason = None
            elif (
                _snorm.match_method == "unmatched"
                and supplier_source in {"header_supplier", "header_contact", "header_candidate", "azure_di_structured"}
                and int(supplier_resolution.get("score") or 0) >= 18
                and int(supplier_resolution.get("support") or 0) >= 1
            ):
                _snorm.match_method = "document_header_vat" if supplier_vat_s3 else "document_header"
                _snorm.match_confidence = max(_snorm.match_confidence, 0.78 if supplier_vat_s3 else 0.72)
                _snorm.review_reason = None
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
        _header_view = extracted.get("_header_text") or ""
        _totals_view = extracted.get("_totals_text") or ""
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
        if _header_view:
            header_raw = _header_view
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
        if _totals_view:
            totals_raw = _totals_view
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
            _filtered_di_totals = _totals_region_text(_di_text, tail_lines=18)
            if _filtered_di_totals:
                totals_raw = _filtered_di_totals
        else:
            page_text_raw = f"[Scanned page — no text layer — extracted via {method}]\n" + header_raw
    else:
        header_raw    = "\n".join(final_text.splitlines()[:12])
        totals_raw    = "\n".join(final_text.splitlines()[-10:])
        if extracted.get("_header_text"):
            header_raw = extracted.get("_header_text")
        if extracted.get("_totals_text"):
            totals_raw = extracted.get("_totals_text")
        page_text_raw = final_text[:20000]

    extracted.update({
        "page_no":                    page_index + 1,
        "method_used":                method + ("+llm_ranked" if any(v == "llm_ranking" for v in (extracted.get("_field_sources") or {}).values()) else ""),
        "extraction_source":          extracted.get("extraction_source") or ("azure_di" if "azure_di" in method else ("native_text" if "native" in method else ("ocr_space" if "ocr_space" in method else "raw_extraction"))),
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
               "_supplier_norm", "_supplier_match_method", "_totals_reconciliation_status",
               "_supplier_resolution"):
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

    invoice_net = page_result.get("net_amount")
    invoice_vat = page_result.get("vat_amount")
    invoice_total = page_result.get("total_amount")

    rows: list[dict[str, Any]] = []
    summed_total = 0.0
    counted = 0
    for idx, line in enumerate(lines, start=1):
        line_total = _line_amount_from_text(line)
        if line_total is not None:
            counted += 1
        row = dict(page_result)
        row["source_invoice_net_amount"] = invoice_net
        row["source_invoice_vat_amount"] = invoice_vat
        row["source_invoice_total_amount"] = invoice_total
        row["description"] = limit_to_20_words(line) or page_result.get("description")
        row["line_items_raw"] = line
        row["line_no"] = idx
        if line_total is not None:
            line_vat = 0.0
            if len(lines) == 1 and invoice_vat not in (None, ""):
                try:
                    line_vat = float(invoice_vat)
                except (TypeError, ValueError):
                    line_vat = 0.0
            row["net_amount"] = line_total
            row["vat_amount"] = line_vat
            row["total_amount"] = round(line_total + line_vat, 2)
            summed_total += row["total_amount"]
        rows.append(row)

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
    invoice_net = page_result.get("net_amount")
    invoice_vat = page_result.get("vat_amount")
    invoice_total = page_result.get("total_amount")
    single_line_vat_fallback = 0.0
    if len(ai_items) == 1 and invoice_vat not in (None, ""):
        try:
            single_line_vat_fallback = float(invoice_vat)
        except (TypeError, ValueError):
            single_line_vat_fallback = 0.0

    for idx, item in enumerate(ai_items, start=1):
        row = dict(page_result)
        row["source_invoice_net_amount"] = invoice_net
        row["source_invoice_vat_amount"] = invoice_vat
        row["source_invoice_total_amount"] = invoice_total

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
            # Use Azure DI's per-line tax if present; otherwise, for a single-line
            # invoice reuse the invoice-level VAT so export rows reconcile cleanly.
            line_vat = item.get("tax_amount")
            if line_vat is not None:
                try:
                    line_vat = float(line_vat)
                except (TypeError, ValueError):
                    line_vat = 0.0
            else:
                line_vat = single_line_vat_fallback
            row["net_amount"] = amount
            row["vat_amount"] = line_vat
            row["total_amount"] = round(amount + line_vat, 2)
            summed += row["total_amount"]

        row["line_no"] = idx
        rows.append(row)

    if not rows:
        return [page_result]

    # Cross-check against the invoice-level gross total, not just net line sums.
    if invoice_total is not None and abs(float(invoice_total) - summed) > 0.10:
        for row in rows:
            row["review_required"] = True
            row["validation_status"] = "review_amount_mismatch"

    return rows


def _build_direct_di_page_rows(
    pdf_path: str | Path,
    page_index: int,
    openai_api_key: str | None = None,
    account_company_name: str | None = None,
) -> list[dict[str, Any]] | None:
    _di_ok, _di_reason = azure_di_available()
    if not _di_ok:
        logger.warning("Direct DI mode skipped: %s", _di_reason)
        return None
    try:
        jpeg_bytes = OCRBackend.render_pdf_page_to_jpeg_bytes(
            Path(pdf_path), page_index, scale=1.5, quality=80
        )
    except Exception as exc:
        logger.warning("Direct DI render failed p%d: %s", page_index, exc)
        return None
    payload = azure_di_extract_invoice(
        jpeg_bytes,
        settings.azure_di_endpoint,
        settings.azure_di_key,
    )
    if not payload:
        return None

    raw_fields = payload.get("_di_raw_fields") or {}
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    raw_payload = payload.get("_di_raw_payload") or {}
    document = raw_payload.get("document") if isinstance(raw_payload, dict) else {}
    document_confidence = (document or {}).get("confidence")

    def field(name: str) -> str | None:
        return _di_direct_text(raw_fields.get(name))

    items = raw_fields.get("Items", {}).get("value_array", [])
    if not isinstance(items, list):
        items = []
    descriptions: list[str] = []
    for item in items:
        item_fields = item.get("value_object") if isinstance(item, dict) else None
        if not isinstance(item_fields, dict):
            continue
        desc = _di_direct_text(item_fields.get("Description"))
        if desc:
            descriptions.append(desc)

    direct_total_source = "InvoiceTotal"
    direct_total = field("InvoiceTotal")
    if direct_total in (None, ""):
        direct_total_source = "SubTotal"
        direct_total = field("SubTotal")
    if direct_total in (None, ""):
        direct_total_source = "AmountDue"
        direct_total = field("AmountDue")
    direct_confidence = None
    try:
        if document_confidence is not None:
            direct_confidence = round(float(document_confidence), 2)
    except Exception:
        direct_confidence = None
    page_text = raw_payload.get("content") if isinstance(raw_payload, dict) else ""

    row = {
        "page_no": page_index + 1,
        "supplier_name": _clean_di_supplier_name(field("VendorName")),
        "supplier_vat": field("VendorTaxId"),
        "supplier_address": field("VendorAddress"),
        "supplier_address_recipient": field("VendorAddressRecipient"),
        "customer_name": field("CustomerName"),
        "customer_vat": field("CustomerTaxId"),
        "customer_address": field("CustomerAddress"),
        "customer_address_recipient": field("CustomerAddressRecipient"),
        "invoice_number": field("InvoiceId"),
        "invoice_date": parse_date(field("InvoiceDate")),
        "_di_raw_invoice_date": field("InvoiceDate"),
        "due_date": parse_date(field("DueDate")),
        "order_number": field("OrderNumber"),
        "purchase_order": field("PurchaseOrder"),
        "description": "; ".join(descriptions),
        "line_items_structured": [],
        "line_items_raw": "\n".join(descriptions),
        "net_amount": parse_amount(field("SubTotal")),
        "vat_amount": parse_amount(field("TotalTax")),
        "total_amount": parse_amount(direct_total),
        "currency": field("CurrencyCode"),
        "tax_code": None,
        "method_used": "DI",
        "extraction_source": "azure_di_direct",
        "confidence_score": direct_confidence,
        "validation_status": "ok",
        "review_required": False,
        "review_priority": None,
        "review_reasons": None,
        "review_fields": None,
        "auto_approved": True,
        "page_quality_score": None,
        "provider_status": "di_success",
        "fallback_used": False,
        "supplier_match_method": "di_direct",
        "totals_reconciliation_status": None,
        "document_type": (document or {}).get("doc_type") or "invoice",
        "raw_di_document_confidence": document_confidence,
        "di_page_text": page_text,
        "_di_structured_fields": {},
        "_di_raw_fields": raw_fields,
        "_di_raw_payload": raw_payload,
        "_direct_di_field_sources": {
            "supplier_name": "VendorName",
            "invoice_number": "InvoiceId",
            "invoice_date": "InvoiceDate",
            "due_date": "DueDate",
            "net_amount": "SubTotal",
            "vat_amount": "TotalTax",
            "total_amount": direct_total_source,
            "currency": "CurrencyCode",
            "customer_name": "CustomerName",
            "customer_vat": "CustomerTaxId",
            "supplier_vat": "VendorTaxId",
            "purchase_order": "PurchaseOrder",
            "order_number": "OrderNumber",
            "description": "Items",
        },
        "_header_text": page_text,
        "_totals_text": "",
        "header_raw": page_text,
        "totals_raw": "",
        "page_text_raw": page_text,
    }
    _apply_direct_di_content_blank_fallbacks(
        row,
        page_text=page_text,
        account_company_name=account_company_name,
    )
    if bool(getattr(settings, "azure_di_read_text_fallback", False)) and not _extraction_has_minimum_invoice_fields(row):
        di_read_payload = azure_di_extract_read_text(
            jpeg_bytes,
            settings.azure_di_endpoint,
            settings.azure_di_key,
        )
        di_read_text = (di_read_payload or {}).get("di_page_text") or ""
        if di_read_text:
            fallback = simple_extract(
                di_read_text,
                openai_api_key=None,
                account_company_name=account_company_name,
            )
            changed = False
            if _fallback_extraction_is_usable(fallback):
                for name in (
                    "supplier_name", "supplier_vat", "invoice_number", "invoice_date",
                    "description", "net_amount", "vat_amount", "total_amount", "currency",
                ):
                    if row.get(name) in (None, "", []) and fallback.get(name) not in (None, "", []):
                        row[name] = fallback.get(name)
                        row["_direct_di_field_sources"][name] = "azure_di_read_text_fallback"
                        changed = True
            row["di_page_text"] = di_read_text
            row["page_text_raw"] = di_read_text
            row["header_raw"] = fallback.get("_header_text") or _header_region_text(di_read_text, max_lines=24)
            row["totals_raw"] = fallback.get("_totals_text") or _totals_region_text(di_read_text, tail_lines=24)
            row["_di_read_raw_payload"] = (di_read_payload or {}).get("_di_raw_payload")
            if changed:
                row["method_used"] = "DI+DI_READ_TEXT_FALLBACK"
                row["extraction_source"] = "azure_di_direct+azure_di_read_text_fallback"
                row["provider_status"] = "di_success_field_fallback_used"
                row["fallback_used"] = True
    try:
        native_text = extract_native_pdf_page(pdf_path, page_index)
    except Exception:
        native_text = None
    _apply_direct_blank_field_fallbacks(
        row,
        pdf_path=pdf_path,
        page_index=page_index,
        native_text=native_text,
        openai_api_key=openai_api_key if settings.use_openai else None,
        account_company_name=account_company_name,
    )
    row["provider_status"] = row.get("_provider_status") or row.get("provider_status")
    row["fallback_used"] = bool(row.get("_fallback_used", row.get("fallback_used")))
    return [row]


def process_pdf_page_rows(
    pdf_path: str | Path,
    page_index: int,
    scan_mode: str = "summary",
    openai_api_key: str | None = None,
    account_company_name: str | None = None,
) -> list[dict[str, Any]]:
    if bool(getattr(settings, "scan_provider_baseline_mode", False)) and bool(getattr(settings, "use_azure_di", False)):
        direct_rows = _build_direct_di_page_rows(
            pdf_path,
            page_index,
            openai_api_key=openai_api_key,
            account_company_name=account_company_name,
        )
        if direct_rows is not None:
            return direct_rows
        skip_azure_di_retry = True
    else:
        skip_azure_di_retry = False

    page_result = process_pdf_page(
        pdf_path,
        page_index=page_index,
        openai_api_key=openai_api_key,
        account_company_name=account_company_name,
        skip_azure_di=skip_azure_di_retry,
    )
    method_text = str(page_result.get("method_used") or "")
    should_mark_di_failed = (
        bool(getattr(settings, "use_azure_di", False))
        and "azure_di" not in method_text.lower()
        and not method_text.startswith("DI")
    )
    if should_mark_di_failed:
        page_result["provider_status"] = "di_failed_fallback_used"
        page_result["fallback_used"] = True
        page_result["review_required"] = True
        page_result["auto_approved"] = False
        page_result["validation_status"] = "review_di_failed_fallback_used"
        reasons = [x for x in re.split(r"[|]", str(page_result.get("review_reasons") or "")) if x]
        if "di_failed_fallback_used" not in reasons:
            reasons.append("di_failed_fallback_used")
        page_result["review_reasons"] = "|".join(reasons)
        existing_method = str(page_result.get("method_used") or "fallback")
        if not existing_method.startswith("DI_FAILED"):
            page_result["method_used"] = f"DI_FAILED+{existing_method}"
        try:
            if page_result.get("confidence_score") is not None:
                page_result["confidence_score"] = min(float(page_result["confidence_score"]), 0.75)
        except Exception:
            page_result["confidence_score"] = 0.75

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

"""Strict invoice date parsing with day-first logic and ambiguity detection.

Core principle: Malta / EU invoices use dd/mm/yyyy by default.  This module
always attempts day-first parsing first and records the strategy used so that
any ambiguity is surfaced rather than silently resolved.

Public API
──────────
    parse_invoice_date(raw: str, supplier_locale: str = "dmy") -> ParsedDate
    format_date(d: date | None) -> str

ParsedDate fields:
    raw             - original string as found on the invoice
    normalized      - date object if parse succeeded, None otherwise
    parse_strategy  - "dmy_unambiguous", "dmy_assumed", "mdy_unambiguous",
                      "mdy_supplier_rule", "iso", "text", "failed"
    ambiguity_flag  - True if day and month are both ≤ 12 (could swap)
    ambiguous_pair  - tuple (dmy_result, mdy_result) when ambiguous, else None
    review_reason   - "ambiguous_date_locale" | "invoice_date_missing" | None
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ── Compiled patterns ─────────────────────────────────────────────────────────

# Numeric: 05/02/2026, 5.2.26, 05-02-2026, 5 02 2026 etc.
_NUM_DATE = re.compile(
    r"(?<!\d)"
    r"(\d{1,2})"            # day or month
    r"[\s./\-\\]"
    r"(\d{1,2})"            # month or day
    r"[\s./\-\\]"
    r"(\d{2,4})"            # year
    r"(?!\d)"
)

# ISO: 2026-02-05
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-](\d{2})[-](\d{2})(?!\d)")

# Text months: 5 February 2026, Feb 5 2026, 05-Feb-26 etc.
_TEXT_MONTH = re.compile(
    r"(?<!\w)"
    r"(\d{1,2})"
    r"[\s\-./]+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
    r"|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"[\s\-./]+"
    r"(\d{2,4})",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class ParsedDate:
    raw: str
    normalized: Optional[date] = None
    parse_strategy: str = "failed"
    ambiguity_flag: bool = False
    ambiguous_pair: Optional[tuple[date, date]] = None
    review_reason: Optional[str] = None


def _make_date(y: int, m: int, d: int) -> Optional[date]:
    """Return a date object or None if values are out of range."""
    try:
        if y < 100:
            y += 2000 if y < 70 else 1900
        if not (1 <= m <= 12 and 1 <= d <= 31):
            return None
        return date(y, m, d)
    except ValueError:
        return None


def _parse_numeric(raw: str, locale: str = "dmy") -> ParsedDate:
    """Parse a numeric date string.  Returns ParsedDate with full metadata."""
    m = _NUM_DATE.search(raw)
    if not m:
        return ParsedDate(raw=raw, parse_strategy="failed",
                          review_reason="invoice_date_missing")

    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))

    # Attempt day-first parse
    dmy = _make_date(y, b, a)   # month=b, day=a
    # Attempt month-first parse
    mdy = _make_date(y, a, b)   # month=a, day=b

    if dmy is None and mdy is None:
        return ParsedDate(raw=raw, parse_strategy="failed",
                          review_reason="invoice_date_missing")

    if dmy is None:
        # Only month-first is valid — unambiguous
        return ParsedDate(raw=raw, normalized=mdy, parse_strategy="mdy_unambiguous",
                          ambiguity_flag=False)

    if mdy is None:
        # Only day-first is valid — unambiguous
        return ParsedDate(raw=raw, normalized=dmy, parse_strategy="dmy_unambiguous",
                          ambiguity_flag=False)

    # Both parses are valid — check if they produce the same date
    if dmy == mdy:
        return ParsedDate(raw=raw, normalized=dmy, parse_strategy="dmy_unambiguous",
                          ambiguity_flag=False)

    # Genuinely ambiguous (day and month both ≤ 12)
    ambiguous = a <= 12 and b <= 12

    if locale == "mdy":
        chosen, strategy = mdy, "mdy_supplier_rule"
    else:
        chosen, strategy = dmy, "dmy_assumed"

    return ParsedDate(
        raw=raw,
        normalized=chosen,
        parse_strategy=strategy,
        ambiguity_flag=ambiguous,
        ambiguous_pair=(dmy, mdy) if ambiguous else None,
        review_reason="ambiguous_date_locale" if ambiguous else None,
    )


def _parse_iso(raw: str) -> Optional[ParsedDate]:
    m = _ISO_DATE.search(raw)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    dt = _make_date(y, mo, d)
    if dt is None:
        return None
    return ParsedDate(raw=raw, normalized=dt, parse_strategy="iso", ambiguity_flag=False)


def _parse_text_month(raw: str) -> Optional[ParsedDate]:
    m = _TEXT_MONTH.search(raw)
    if not m:
        return None
    day_s, month_s, year_s = m.group(1), m.group(2), m.group(3)
    mo = _MONTH_MAP.get(month_s[:3].lower())
    if mo is None:
        return None
    dt = _make_date(int(year_s), mo, int(day_s))
    if dt is None:
        return None
    return ParsedDate(raw=raw, normalized=dt, parse_strategy="text", ambiguity_flag=False)


def parse_invoice_date(
    raw: str | None,
    supplier_locale: str = "dmy",
) -> ParsedDate:
    """Parse an invoice date string into a structured ParsedDate.

    Args:
        raw: The raw date string extracted from the invoice (may be None).
        supplier_locale: "dmy" (default, EU) or "mdy" (US-format supplier).

    Returns:
        ParsedDate with normalized date, strategy, and ambiguity metadata.

    Examples:
        >>> parse_invoice_date("05/02/2026").normalized
        datetime.date(2026, 2, 5)   # 5 February — day-first

        >>> parse_invoice_date("05/02/2026").ambiguity_flag
        True   # both 5 Feb and 2 May are valid — review flagged
    """
    if not raw or not raw.strip():
        return ParsedDate(raw=raw or "", parse_strategy="failed",
                          review_reason="invoice_date_missing")

    raw = raw.strip()

    # 1. ISO format (yyyy-mm-dd) — unambiguous
    iso = _parse_iso(raw)
    if iso:
        return iso

    # 2. Text month — unambiguous
    text = _parse_text_month(raw)
    if text:
        return text

    # 3. Numeric — may be ambiguous
    return _parse_numeric(raw, supplier_locale)


def format_date(d: Optional[date]) -> str:
    """Format a date as dd/mm/yyyy or empty string."""
    if d is None:
        return ""
    return d.strftime("%d/%m/%Y")

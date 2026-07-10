from __future__ import annotations

import re


GENERIC_DESCRIPTIONS = {
    "",
    "items",
    "invoice line",
    "invoice goods or services",
    "invoice extraction",
}


def _compact(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _limit_words(text: str, max_words: int = 14) -> str:
    words = re.findall(r"\S+", _compact(text))
    return " ".join(words[:max_words]).strip()


def _clean_item_line(line: str) -> str:
    text = _compact(line)
    text = re.sub(r"\b(?:eur|euro|€)\s*\d+(?:[.,]\d{2})?\b", "", text, flags=re.I)
    text = re.sub(r"\b\d+(?:[.,]\d{2})\b", "", text)
    text = re.sub(r"\b(?:qty|quantity|unit|price|total|amount|vat|tax|code|barcode)\b[:\s-]*", "", text, flags=re.I)
    text = re.sub(r"\s{2,}", " ", text).strip(" -;,.")
    return text


def _has_term(text: str, term: str) -> bool:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.I) is not None


def summarise_total_invoice_description(description: object, line_items_raw: object = None) -> str | None:
    """Return a concise purchase summary for one-row total-invoice exports.

    Keeps concise human descriptions as-is. When the source description is
    blank, generic, or too long for review/export tables, derives a short
    category or item summary from line-item text without supplier-specific rules.
    """
    current = _compact(description)
    current_key = current.lower()
    if current and current_key not in GENERIC_DESCRIPTIONS and len(current) <= 80:
        return current

    source = _compact(line_items_raw) or current
    lower = source.lower()
    keyword_groups = [
        ("fuel and vehicle consumables", ["fuel", "diesel", "petrol", "unleaded", "lubricant"]),
        ("office supplies and stationery", ["paper", "stationery", "toner", "ink", "folder", "pen", "notebook"]),
        ("cleaning and hygiene supplies", ["detergent", "cleaner", "soap", "bleach", "sanitiser", "tissue"]),
        ("food and beverage supplies", [
            "food", "catering", "beverage", "drink", "snack", "bread", "meat", "chicken",
            "beef", "fish", "seafood", "dairy", "cheese", "milk", "eggs", "wine", "beer",
            "spirits", "coffee", "tea", "juice", "water", "soft drink", "frozen", "produce",
        ]),
        ("vehicle parts and maintenance", ["filter", "brake", "tyre", "battery", "engine", "service kit"]),
        ("electrical supplies and components", ["cable", "socket", "switch", "lamp", "electrical", "fuse"]),
        ("building materials and hardware", ["cement", "paint", "screw", "bolt", "hardware", "tool"]),
        ("printing and marketing materials", ["print", "printing", "flyer", "poster", "banner", "brochure"]),
        ("software subscriptions or digital services", ["subscription", "software", "license", "hosting", "domain"]),
        ("professional or business services", ["service", "consulting", "labour", "maintenance", "support"]),
    ]
    for label, terms in keyword_groups:
        if any(_has_term(lower, term) for term in terms):
            return label

    lines = [_clean_item_line(line) for line in str(source or "").splitlines()]
    lines = [line for line in lines if len(line) > 3 and not re.fullmatch(r"[\d\s.,€£$%-]+", line)]
    unique_lines = list(dict.fromkeys(lines))
    if unique_lines:
        return _limit_words("; ".join(unique_lines[:3]))

    if current:
        return _limit_words(current)
    return "Invoice goods or services"

"""Supplier name normalization and canonical matching.

Normalizes OCR-noisy supplier names and matches them against the supplier
master in config/suppliers.yaml.  Returns structured SupplierNorm objects
with raw, clean, canonical names plus match method and confidence score.

Public API
──────────
    normalize_supplier(raw: str | None) -> SupplierNorm
    load_supplier_config() -> list[dict]     (cached)

SupplierNorm fields:
    raw                  - original string as extracted
    clean                - OCR artefacts removed, whitespace normalized
    canonical            - matched canonical name (or clean if unmatched)
    match_method         - "vat_match" | "alias_match" | "fuzzy_match" | "unmatched"
    match_confidence     - 0.0–1.0
    supplier_locale      - "dmy" | "mdy" (from config, default "dmy")
    nominal_hint         - suggested nominal category from config
    product_families     - list of expected product families
    review_reason        - "supplier_unknown" | "supplier_low_confidence" | None
"""
from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "suppliers.yaml"


@dataclass
class SupplierNorm:
    raw: str
    clean: str
    canonical: str
    match_method: str = "unmatched"
    match_confidence: float = 0.0
    supplier_locale: str = "dmy"
    nominal_hint: str = ""
    product_families: list[str] = field(default_factory=list)
    review_reason: Optional[str] = None


# ── Config loader ──────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def load_supplier_config() -> list[dict]:
    """Load suppliers.yaml once and cache it."""
    try:
        import yaml  # type: ignore
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        suppliers = data.get("suppliers", []) if data else []
        logger.debug("Loaded %d supplier entries from config", len(suppliers))
        return suppliers
    except FileNotFoundError:
        logger.warning("suppliers.yaml not found at %s", _CONFIG_PATH)
        return []
    except Exception as exc:
        logger.warning("Failed to load suppliers.yaml: %s", exc)
        return []


# ── Text cleaning ─────────────────────────────────────────────────────────────

# Characters that are commonly confused or inserted by OCR
_OCR_NOISE = re.compile(
    r"[|\\#@~`^]"            # pipe, backslash, hash, etc.
    r"|(?<!\w)[\d]{1,2}(?!\w)"  # lone 1-2 digit numbers (page numbers bleeding in)
)
_WHITESPACE = re.compile(r"\s+")
_LEADING_PUNCT = re.compile(r"^[\W_]+")
_TRAILING_PUNCT = re.compile(r"[\W_]+$")

# Common suffixes that don't distinguish suppliers
_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|llc|llp|inc|co|company|group|holdings|malta|mt|"
    r"gmbh|sarl|bv|nv|ag|spa|srl)\b",
    re.IGNORECASE,
)


def clean_supplier_name(raw: str | None) -> str:
    """Remove OCR artefacts, normalize whitespace, strip leading/trailing junk."""
    if not raw:
        return ""
    text = raw.replace("\n", " ").replace("\r", " ")
    text = _OCR_NOISE.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _LEADING_PUNCT.sub("", text).strip()
    text = _TRAILING_PUNCT.sub("", text).strip()
    return text


def _core_words(name: str) -> set[str]:
    """Extract meaningful words (remove suffixes and stopwords) for fuzzy matching."""
    words = _WHITESPACE.split(_SUFFIXES.sub("", name.lower()))
    stopwords = {"the", "a", "of", "and", "&", "for"}
    return {w for w in words if len(w) > 2 and w not in stopwords}


def _word_overlap(a: str, b: str) -> float:
    wa = _core_words(a)
    wb = _core_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


# ── Matching ──────────────────────────────────────────────────────────────────

def normalize_supplier(
    raw: str | None,
    supplier_vat: str | None = None,
) -> SupplierNorm:
    """Normalize a raw supplier name and match against the supplier master.

    Matching order:
        1. VAT number exact match (if provided)
        2. Alias exact match (case-insensitive)
        3. Fuzzy word-overlap match (threshold 0.60)

    Args:
        raw:          Raw supplier name as extracted by OCR/AI.
        supplier_vat: VAT number extracted from the document (optional).

    Returns:
        SupplierNorm with all matching metadata.
    """
    raw_str = (raw or "").strip()
    clean = clean_supplier_name(raw_str)
    clean_lower = clean.lower()

    suppliers = load_supplier_config()
    if not suppliers:
        # No config available — return what we have
        return SupplierNorm(
            raw=raw_str, clean=clean, canonical=clean or raw_str,
            match_method="unmatched", match_confidence=0.0,
            review_reason="supplier_unknown" if not clean else None,
        )

    # 1. VAT number match
    if supplier_vat:
        vat_clean = re.sub(r"\s+", "", supplier_vat).upper()
        for s in suppliers:
            sv = re.sub(r"\s+", "", (s.get("vat_number") or "")).upper()
            if sv and sv == vat_clean:
                return SupplierNorm(
                    raw=raw_str, clean=clean, canonical=s["canonical"],
                    match_method="vat_match", match_confidence=1.0,
                    supplier_locale=s.get("date_locale", "dmy"),
                    nominal_hint=s.get("nominal_hint", ""),
                    product_families=s.get("product_families", []),
                )

    if not clean:
        return SupplierNorm(
            raw=raw_str, clean="", canonical="",
            match_method="unmatched", match_confidence=0.0,
            review_reason="supplier_unknown",
        )

    # 2. Alias exact match
    for s in suppliers:
        aliases = [a.lower() for a in (s.get("aliases") or [])]
        canonical_lower = s["canonical"].lower()
        if clean_lower == canonical_lower or clean_lower in aliases:
            return SupplierNorm(
                raw=raw_str, clean=clean, canonical=s["canonical"],
                match_method="alias_match", match_confidence=0.95,
                supplier_locale=s.get("date_locale", "dmy"),
                nominal_hint=s.get("nominal_hint", ""),
                product_families=s.get("product_families", []),
            )

    # Partial alias containment
    for s in suppliers:
        aliases = [a.lower() for a in (s.get("aliases") or [])]
        canonical_lower = s["canonical"].lower()
        candidates = [canonical_lower] + aliases
        if any(c in clean_lower or clean_lower in c for c in candidates):
            return SupplierNorm(
                raw=raw_str, clean=clean, canonical=s["canonical"],
                match_method="alias_match", match_confidence=0.80,
                supplier_locale=s.get("date_locale", "dmy"),
                nominal_hint=s.get("nominal_hint", ""),
                product_families=s.get("product_families", []),
            )

    # 3. Fuzzy word-overlap match
    best_score, best_supplier = 0.0, None
    for s in suppliers:
        score = _word_overlap(clean, s["canonical"])
        # Also check against aliases
        for alias in s.get("aliases") or []:
            score = max(score, _word_overlap(clean, alias))
        if score > best_score:
            best_score = score
            best_supplier = s

    if best_score >= 0.60 and best_supplier:
        return SupplierNorm(
            raw=raw_str, clean=clean, canonical=best_supplier["canonical"],
            match_method="fuzzy_match", match_confidence=round(best_score, 2),
            supplier_locale=best_supplier.get("date_locale", "dmy"),
            nominal_hint=best_supplier.get("nominal_hint", ""),
            product_families=best_supplier.get("product_families", []),
            review_reason="supplier_low_confidence" if best_score < 0.75 else None,
        )

    # 4. Unmatched
    return SupplierNorm(
        raw=raw_str, clean=clean, canonical=clean or raw_str,
        match_method="unmatched", match_confidence=round(best_score, 2),
        review_reason="supplier_unknown" if not clean else "supplier_low_confidence",
    )

"""Hybrid line classification engine.

Classification order (first match wins, LLM last resort):
    1. supplier_rule    — supplier config nominal_hint maps directly
    2. taxonomy_match   — brand/product in product_taxonomy.yaml
    3. historical_similarity — most-used nominal for this supplier (via DB query hook)
    4. llm_fallback     — OpenAI semantic fallback (when configured)

Public API
──────────
    classify_line(
        description: str | None,
        line_items_raw: str | None,
        supplier_norm: SupplierNorm | None,
        nominal_accounts: list[dict],
        historical_hook: Callable | None,
        openai_api_key: str | None,
    ) -> LineClassification

LineClassification fields:
    nominal_account_code      - matched code or None
    nominal_account_name      - matched name or None
    classification_method     - supplier_rule | taxonomy_match | historical_similarity
                                | llm_fallback | keyword_match | default | unresolved
    classification_reason     - human-readable explanation
    classification_confidence - 0.0–1.0
    brand                     - matched brand if taxonomy matched
    product_family            - product family if matched
    category                  - category if matched
    subcategory               - subcategory if matched
"""
from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_TAXONOMY_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "product_taxonomy.yaml"


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class LineClassification:
    nominal_account_code: Optional[str] = None
    nominal_account_name: Optional[str] = None
    classification_method: str = "unresolved"
    classification_reason: str = ""
    classification_confidence: float = 0.0
    brand: Optional[str] = None
    product_family: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None


# ── Taxonomy loader ────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _load_taxonomy() -> dict:
    """Load product_taxonomy.yaml (cached)."""
    try:
        import yaml  # type: ignore
        with open(_TAXONOMY_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data.get("categories", {}) if data else {}
    except FileNotFoundError:
        logger.warning("product_taxonomy.yaml not found at %s", _TAXONOMY_PATH)
        return {}
    except Exception as exc:
        logger.warning("Failed to load product_taxonomy.yaml: %s", exc)
        return {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _best_nominal(
    hint: str,
    nominal_accounts: list[dict],
) -> tuple[str | None, str | None]:
    """Return (code, name) of the first nominal whose name contains hint."""
    if not hint:
        return None, None
    hint_lower = hint.lower()
    for acct in nominal_accounts:
        if hint_lower in (acct.get("account_name") or "").lower():
            return acct.get("account_code"), acct.get("account_name")
    return None, None


def _find_taxonomy_match(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Search taxonomy aliases in text.

    Returns:
        (category, nominal_hint, brand, product_family, subcategory) or all None
    """
    taxonomy = _load_taxonomy()
    text_lower = text.lower()

    for category, cat_data in taxonomy.items():
        for entry in cat_data.get("entries", []):
            aliases = entry.get("aliases") or []
            # Test each alias — longest aliases first to prefer specificity
            for alias in sorted(aliases, key=len, reverse=True):
                if alias in text_lower:
                    return (
                        category,
                        cat_data.get("nominal_hint", category),
                        entry.get("brand"),
                        entry.get("product_family"),
                        entry.get("subcategory"),
                    )
    return None, None, None, None, None


def _keyword_match(
    text: str,
    nominal_accounts: list[dict],
) -> tuple[str | None, str | None]:
    """Check if any nominal account name/code appears in the description text."""
    text_lower = text.lower()
    for acct in nominal_accounts:
        name = (acct.get("account_name") or "").lower()
        code = (acct.get("account_code") or "").lower()
        if (name and name in text_lower) or (code and code in text_lower):
            return acct.get("account_code"), acct.get("account_name")
    return None, None


# ── Main classification function ───────────────────────────────────────────────

def classify_line(
    description: str | None = None,
    line_items_raw: str | None = None,
    supplier_norm=None,          # SupplierNorm or None (avoid circular import)
    nominal_accounts: list[dict] | None = None,
    historical_hook: Callable | None = None,
    openai_api_key: str | None = None,
) -> LineClassification:
    """Classify a line item using the hybrid classification pipeline.

    Args:
        description:     Invoice description field.
        line_items_raw:  Raw line items text.
        supplier_norm:   Normalized supplier object (SupplierNorm) or None.
        nominal_accounts: List of {account_code, account_name} dicts.
        historical_hook: Callable() → str | None — returns most-used nominal
                         for this supplier from historical invoice rows.
        openai_api_key:  OpenAI key for LLM fallback (None to skip).

    Returns:
        LineClassification with all fields populated.
    """
    accounts = nominal_accounts or []
    default_account = next(
        (a for a in accounts if a.get("is_default")), None
    )

    search_text = " ".join(filter(None, [description, line_items_raw]))

    # ── Step 1: Supplier rule ────────────────────────────────────────────────
    # If the supplier config defines a nominal_hint, prefer it unconditionally
    # over other methods (accountant-configured per-supplier rule).
    if supplier_norm and getattr(supplier_norm, "nominal_hint", None):
        code, name = _best_nominal(supplier_norm.nominal_hint, accounts)
        if code:
            return LineClassification(
                nominal_account_code=code,
                nominal_account_name=name,
                classification_method="supplier_rule",
                classification_reason=(
                    f"Supplier config nominal_hint '{supplier_norm.nominal_hint}' "
                    f"mapped to account {code}"
                ),
                classification_confidence=0.95,
            )

    # ── Step 2: Taxonomy / brand match ───────────────────────────────────────
    if search_text:
        category, nominal_hint, brand, product_family, subcategory = \
            _find_taxonomy_match(search_text)
        if category and nominal_hint:
            code, name = _best_nominal(nominal_hint, accounts)
            if code:
                return LineClassification(
                    nominal_account_code=code,
                    nominal_account_name=name,
                    classification_method="taxonomy_match",
                    classification_reason=(
                        f"Brand/product '{brand or category}' matched taxonomy "
                        f"category '{category}' → account {code}"
                    ),
                    classification_confidence=0.90,
                    brand=brand,
                    product_family=product_family,
                    category=category,
                    subcategory=subcategory,
                )

    # ── Step 3: Historical similarity ────────────────────────────────────────
    if historical_hook is not None:
        hist_code = historical_hook()
        if hist_code:
            # Find the account name
            hist_name = next(
                (a.get("account_name") for a in accounts
                 if a.get("account_code") == hist_code),
                None,
            )
            return LineClassification(
                nominal_account_code=hist_code,
                nominal_account_name=hist_name,
                classification_method="historical_similarity",
                classification_reason=(
                    f"Most-used nominal for this supplier historically: {hist_code}"
                ),
                classification_confidence=0.80,
            )

    # ── Step 4: Keyword match (description contains account name/code) ────────
    if search_text:
        code, name = _keyword_match(search_text, accounts)
        if code:
            return LineClassification(
                nominal_account_code=code,
                nominal_account_name=name,
                classification_method="keyword_match",
                classification_reason=(
                    f"Account name/code found in description text → {code}"
                ),
                classification_confidence=0.70,
            )

    # ── Step 5: LLM fallback ────────────────────────────────────────────────
    # Only if OpenAI is configured and we have meaningful text to send.
    if openai_api_key and search_text and len(search_text) > 10 and accounts:
        try:
            result = _llm_classify(search_text, accounts, openai_api_key)
            if result:
                return result
        except Exception as exc:
            logger.warning("LLM classification fallback failed: %s", exc)

    # ── Step 6: Default account ──────────────────────────────────────────────
    if default_account:
        return LineClassification(
            nominal_account_code=default_account.get("account_code"),
            nominal_account_name=default_account.get("account_name"),
            classification_method="default",
            classification_reason="No specific match — using marked default nominal account",
            classification_confidence=0.40,
        )

    return LineClassification(
        classification_method="unresolved",
        classification_reason="No nominal account match could be determined",
        classification_confidence=0.0,
    )


# ── LLM fallback ──────────────────────────────────────────────────────────────

def _llm_classify(
    text: str,
    nominal_accounts: list[dict],
    api_key: str,
) -> LineClassification | None:
    """Ask OpenAI to pick the best nominal account code for the line item.

    This is strictly a last-resort fallback.  The LLM is given the list of
    available accounts and the description text — it is NOT allowed to invent
    codes or names.
    """
    try:
        import json
        import openai  # type: ignore

        client = openai.OpenAI(api_key=api_key)

        accounts_str = "\n".join(
            f"  {a.get('account_code')}: {a.get('account_name')}"
            for a in nominal_accounts[:30]
        )
        prompt = (
            "You are an accounting classification assistant.  "
            "Given the invoice line description below, pick the SINGLE best "
            "matching nominal account code from the list.  "
            "Respond ONLY with JSON: {\"code\": \"...\", \"reason\": \"...\"}.  "
            "Do not invent codes.  If no code fits, return {\"code\": null}.\n\n"
            f"Description: {text[:500]}\n\n"
            f"Available accounts:\n{accounts_str}"
        )

        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
        )

        result = json.loads(resp.choices[0].message.content or "{}")
        code = result.get("code")
        reason = result.get("reason", "LLM classification")

        if not code:
            return None

        name = next(
            (a.get("account_name") for a in nominal_accounts
             if a.get("account_code") == code),
            None,
        )
        if name is None:
            return None  # LLM invented a code that doesn't exist

        return LineClassification(
            nominal_account_code=code,
            nominal_account_name=name,
            classification_method="llm_fallback",
            classification_reason=f"LLM: {reason}",
            classification_confidence=0.65,
        )

    except Exception as exc:
        logger.warning("_llm_classify: %s", exc)
        return None

"""Review engine — weighted triage with priority bands and auto-approval.

Consolidates all reason codes from extraction, normalization, classification,
and validation into a single authoritative review decision.

Priority bands
──────────────
    high   — severe failures that prevent posting (supplier unknown, major
             totals mismatch, AI validation hard-failure, no amount)
    medium — ambiguities that a reviewer should check but don't block posting
             (ambiguous date, low-confidence supplier, partial mismatch)
    low    — minor OCR issues or weak evidence that auto-approved rows carry
             as informational flags

Public API
──────────
    compute_review_decision(
        extracted: dict,
        supplier_reasons: list[str],
        validation: InvoiceValidation | None,
        classification: LineClassification | None,
        confidence: float,
        page_quality: float,
    ) -> ReviewDecision

ReviewDecision fields:
    review_required     - bool
    review_priority     - "high" | "medium" | "low" | None
    review_reasons      - ordered list of reason codes (highest severity first)
    review_fields       - list of field names that contributed to review
    auto_approved       - True if all issues are low-severity and auto-approvable
    validation_status   - final status string for the DB field
    manual_override_status - None (set by human later)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Severity weights ──────────────────────────────────────────────────────────
# Higher score = higher severity = more likely to require human review.

_REASON_WEIGHTS: dict[str, tuple[int, str, list[str]]] = {
    # (weight, priority_band, fields_affected)
    "no_supplier":               (100, "high",   ["supplier_name"]),
    "no_amount":                 (100, "high",   ["total_amount"]),
    "ai_validation_failed":      (90,  "high",   ["_general"]),
    "totals_mismatch":           (80,  "high",   ["net_amount", "vat_amount", "total_amount"]),
    "vat_anomaly":               (70,  "high",   ["vat_amount"]),
    "supplier_unknown":          (65,  "high",   ["supplier_name"]),
    "invoice_number_missing":    (60,  "medium", ["invoice_number"]),
    "invoice_date_missing":      (55,  "medium", ["invoice_date"]),
    "vat_missing":               (50,  "medium", ["vat_amount"]),
    "ambiguous_date_locale":     (45,  "medium", ["invoice_date"]),
    "supplier_low_confidence":   (40,  "medium", ["supplier_name"]),
    "line_classification_unresolved": (35, "medium", ["nominal_account_code"]),
    "nominal_mapping_uncertain": (30,  "medium", ["nominal_account_code"]),
    "ai_validation_warned":      (25,  "medium", ["_general"]),
    "low_confidence":            (20,  "medium", ["confidence_score"]),
    "page_quality_low":          (15,  "low",    ["_general"]),
    "subtotal_not_found":        (15,  "low",    ["net_amount"]),
    "deposit_component_detected":(10,  "low",    ["total_amount"]),
    "totals_mismatch_advisory":  (35,  "medium", ["net_amount", "vat_amount", "total_amount"]),
    "line_sum_mismatch":         (10,  "low",    ["line_items_raw"]),
    "mixed_category_invoice":    (10,  "low",    ["nominal_account_code"]),
    "low_evidence_density":      (5,   "low",    ["_general"]),
}

# Reasons in this set may be auto-approved if no high/medium reasons are present
_AUTO_APPROVABLE_REASONS = {
    "page_quality_low",
    "line_sum_mismatch",
    "mixed_category_invoice",
    "low_evidence_density",
}


@dataclass
class ReviewDecision:
    review_required: bool = False
    review_priority: Optional[str] = None
    review_reasons: list[str] = field(default_factory=list)
    review_fields: list[str] = field(default_factory=list)
    auto_approved: bool = False
    validation_status: str = "ok"
    manual_override_status: Optional[str] = None


def compute_review_decision(
    extracted: dict,
    supplier_reasons: list[str] | None = None,
    validation=None,           # InvoiceValidation | None
    classification=None,       # LineClassification | None
    confidence: float = 1.0,
    page_quality: float = 1.0,
) -> ReviewDecision:
    """Consolidate all reason codes into a single review decision.

    Args:
        extracted:         The extraction result dict (from process_pdf_page).
        supplier_reasons:  Reasons from supplier normalization (SupplierNorm).
        validation:        InvoiceValidation object (from validate_invoice).
        classification:    LineClassification object (from classify_line).
        confidence:        Overall extraction confidence 0–1.
        page_quality:      Page quality score 0–1 (from preprocessing).

    Returns:
        ReviewDecision with priority, reasons, fields, and validation_status.
    """
    all_reasons: list[str] = []

    # ── Collect reasons from all stages ──────────────────────────────────────
    # From supplier normalization
    if supplier_reasons:
        all_reasons.extend(supplier_reasons)

    # From validation
    if validation is not None:
        all_reasons.extend(validation.review_reasons)

    # From classification
    if classification is not None:
        if classification.classification_method == "unresolved":
            all_reasons.append("line_classification_unresolved")
        elif classification.classification_method in ("default", "llm_fallback"):
            all_reasons.append("nominal_mapping_uncertain")

    # From confidence and quality
    if confidence < 0.55:
        all_reasons.append("low_confidence")
    if page_quality < 0.35:
        all_reasons.append("page_quality_low")

    # From extraction fields
    if not extracted.get("supplier_name"):
        if "no_supplier" not in all_reasons:
            all_reasons.append("no_supplier")
    if extracted.get("total_amount") is None:
        if "no_amount" not in all_reasons:
            all_reasons.append("no_amount")
    if not extracted.get("invoice_number"):
        if "invoice_number_missing" not in all_reasons:
            all_reasons.append("invoice_number_missing")
    if not extracted.get("invoice_date"):
        if "invoice_date_missing" not in all_reasons:
            all_reasons.append("invoice_date_missing")

    # AI validation result
    val_result = extracted.get("_validation_result") or {}
    val_status_ai = val_result.get("validated_status", "passed") if val_result else "passed"
    if val_status_ai == "failed" and "ai_validation_failed" not in all_reasons:
        all_reasons.append("ai_validation_failed")
    elif val_status_ai == "passed_with_warnings" and "ai_validation_warned" not in all_reasons:
        all_reasons.append("ai_validation_warned")

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in all_reasons:
        key = r.split(":")[0]  # strip deposit amount suffix
        if key not in seen:
            seen.add(key)
            unique_reasons.append(r)

    # ── Score and sort reasons by severity ───────────────────────────────────
    def _weight(reason: str) -> int:
        key = reason.split(":")[0]
        return _REASON_WEIGHTS.get(key, (0, "low", []))[0]

    sorted_reasons = sorted(unique_reasons, key=_weight, reverse=True)

    # ── Determine priority band ───────────────────────────────────────────────
    max_priority = None
    affected_fields: list[str] = []
    for reason in sorted_reasons:
        key = reason.split(":")[0]
        _, priority, fields = _REASON_WEIGHTS.get(key, (0, "low", []))
        if max_priority is None:
            max_priority = priority
        affected_fields.extend(f for f in fields if f not in affected_fields)

    # Hierarchy: high > medium > low
    _BAND_ORDER = {"high": 2, "medium": 1, "low": 0}

    # ── Determine validation_status ───────────────────────────────────────────
    high_reasons = {r for r in sorted_reasons if _REASON_WEIGHTS.get(r.split(":")[0], (0, "low", []))[1] == "high"}
    medium_reasons = {r for r in sorted_reasons if _REASON_WEIGHTS.get(r.split(":")[0], (0, "low", []))[1] == "medium"}

    if not sorted_reasons:
        final_status = "ok"
        review_required = False
        priority = None
    elif high_reasons:
        review_required = True
        priority = "high"
        if "no_supplier" in high_reasons and "no_amount" in high_reasons:
            final_status = "review_incomplete"
        elif "no_supplier" in high_reasons:
            final_status = "review_no_supplier"
        elif "no_amount" in high_reasons:
            final_status = "review_no_amount"
        elif "ai_validation_failed" in high_reasons:
            final_status = "review_validation_failed"
        else:
            final_status = "review_validation_failed"
    elif medium_reasons:
        review_required = False
        priority = "medium"
        final_status = "ok_warned"
    else:
        review_required = False
        priority = "low"
        final_status = "ok"

    # ── Auto-approval ─────────────────────────────────────────────────────────
    # A row is auto-approvable when all its reasons are in the low-severity set
    reason_keys = {r.split(":")[0] for r in sorted_reasons}
    auto_approved = (
        not review_required
        and bool(reason_keys)
        and reason_keys.issubset(_AUTO_APPROVABLE_REASONS)
    )

    return ReviewDecision(
        review_required=review_required,
        review_priority=priority,
        review_reasons=sorted_reasons,
        review_fields=list(dict.fromkeys(affected_fields)),  # dedupe, preserve order
        auto_approved=auto_approved,
        validation_status=final_status,
    )

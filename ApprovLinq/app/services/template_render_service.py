from __future__ import annotations

import decimal
import logging
import uuid
from datetime import date, datetime

logger = logging.getLogger(__name__)

AVAILABLE_FIELDS: list[str] = [
    "source_filename",
    "page_no",
    "supplier_name",
    "supplier_posting_account",
    "invoice_number",
    "invoice_date",
    "due_date",
    "description",
    "line_items_raw",
    "net_amount",
    "vat_amount",
    "total_amount",
    "currency",
    "tax_code",
    "method_used",
    "confidence_score",
    "page_quality_score",
    "validation_status",
    "review_required",
    "review_priority",
    "review_reasons",
    "review_fields",
    "auto_approved",
    "nominal_account_code",
    "nominal_account_name",
    "classification_method",
    "supplier_match_method",
    "totals_reconciliation_status",
    "batch_id",
    "company_name",
    "tenant_name",
    "header_raw",
    "totals_raw",
]

COLUMN_TYPES: set[str] = {
    "mapped_field",
    "static_text",
    "empty_column",
    "derived_value",
    "conditional_value",
}

TRANSFORM_RULES: list[str] = [
    "uppercase",
    "lowercase",
    "number_format",
    "date_format:dd/mm/yyyy",
    "date_format:%Y-%m-%d",
    "default:<value>",
]


def apply_transform(value, rule: str | None):
    """Apply a named transform to a cell value. Returns transformed value or original."""
    if not rule:
        return value
    r = rule.strip()
    rl = r.lower()

    if rl == "uppercase":
        return str(value).upper() if value is not None else None

    if rl == "lowercase":
        return str(value).lower() if value is not None else None

    if rl.startswith("date_format:"):
        fmt = r.split(":", 1)[1].strip()
        if isinstance(value, (date, datetime)):
            try:
                return value.strftime(fmt)
            except ValueError:
                return value
        return value

    if rl == "number_format":
        try:
            return float(value)
        except (ValueError, TypeError):
            return value

    if rl.startswith("default:"):
        fallback = r.split(":", 1)[1]
        if value is None or str(value).strip() == "":
            return fallback
        return value

    return value


def _coerce_cell(value):
    """Convert any Python value to an openpyxl-safe equivalent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def _sanitize_sheet_name(name: str) -> str:
    """Excel sheet names: max 31 chars, no :\\/?*[]"""
    result = name[:31]
    for ch in r":\/?*[]":
        result = result.replace(ch, "_")
    return result.strip() or "Export"


def render_template_sheet(
    template,
    rows: list[dict],
    enrichment: dict | None = None,
) -> tuple[str, list[dict]]:
    """
    Render an ExportTemplate against a list of invoice-row dicts.

    Args:
        template:    ExportTemplate ORM object (columns relationship must be loaded).
        rows:        List of dicts keyed by field names (one per invoice row).
        enrichment:  Optional extra fields merged into every row
                     (e.g. ``{"company_name": "Acme Ltd", "tenant_name": "Demo"}``).

    Returns:
        (sheet_name, list_of_ordered_row_dicts)
        Each row dict keys are the column headings defined in the template.
    """
    enrichment = enrichment or {}
    active_cols = sorted(
        [c for c in (template.columns or []) if c.is_active],
        key=lambda c: c.column_order,
    )
    sheet_name = _sanitize_sheet_name(template.name)
    result_rows = []

    for row in rows:
        merged = {**enrichment, **row}
        out_row: dict = {}

        for col in active_cols:
            heading = col.column_heading
            ctype = col.column_type

            if ctype == "static_text":
                out_row[heading] = col.static_value or ""

            elif ctype == "empty_column":
                out_row[heading] = None

            elif ctype in ("mapped_field", "derived_value", "conditional_value"):
                raw = merged.get(col.source_field) if col.source_field else None
                transformed = apply_transform(raw, col.transform_rule)
                out_row[heading] = _coerce_cell(transformed)

            else:
                out_row[heading] = None

        result_rows.append(out_row)

    return sheet_name, result_rows


def resolve_effective_template(db, tenant_id, company_id):
    """
    Resolve the effective export template for a tenant/company pair.

    Precedence:
    1. Active company-level assignment  (company_id IS NOT NULL and matches)
    2. Active tenant-level assignment   (company_id IS NULL)
    3. None — no template sheet added to the workbook.

    Args:
        db:         SQLAlchemy Session.
        tenant_id:  UUID of the tenant.
        company_id: UUID of the company, or None.

    Returns:
        ExportTemplate ORM object, or None.
    """
    from app.db.models import ExportTemplate, TemplateAssignment

    if company_id:
        assign = (
            db.query(TemplateAssignment)
            .filter(
                TemplateAssignment.tenant_id == tenant_id,
                TemplateAssignment.company_id == company_id,
                TemplateAssignment.is_active.is_(True),
            )
            .first()
        )
        if assign:
            tpl = db.get(ExportTemplate, assign.template_id)
            if tpl and tpl.is_active:
                return tpl

    assign = (
        db.query(TemplateAssignment)
        .filter(
            TemplateAssignment.tenant_id == tenant_id,
            TemplateAssignment.company_id.is_(None),
            TemplateAssignment.is_active.is_(True),
        )
        .first()
    )
    if assign:
        tpl = db.get(ExportTemplate, assign.template_id)
        if tpl and tpl.is_active:
            return tpl

    return None

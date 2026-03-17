from __future__ import annotations
import decimal
import uuid
from datetime import date, datetime
from io import BytesIO

import pandas as pd


def _safe_value(v):
    """Convert types that openpyxl cannot write into Excel-safe equivalents."""
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v
    return v


def _row_to_dict(row) -> dict:
    if isinstance(row, dict):
        return {k: _safe_value(v) for k, v in row.items()}
    return {col: _safe_value(getattr(row, col)) for col in row.__table__.columns.keys()}


def workbook_from_rows(rows) -> BytesIO:
    row_dicts = [_row_to_dict(r) for r in rows]
    df = pd.DataFrame(row_dicts)

    if df.empty:
        df = pd.DataFrame(columns=[
            "source_filename",
            "page_no",
            "supplier_name",
            "invoice_number",
            "invoice_date",
            "description",
            "line_items_raw",
            "net_amount",
            "vat_amount",
            "total_amount",
            "currency",
            "tax_code",
            "method_used",
            "confidence_score",
            "validation_status",
            "review_required",
        ])

    preferred_order = [
        "source_filename",
        "page_no",
        "supplier_name",
        "supplier_posting_account",
        "nominal_account_code",
        "invoice_number",
        "invoice_date",
        "description",
        "line_items_raw",
        "net_amount",
        "vat_amount",
        "total_amount",
        "currency",
        "tax_code",
        "method_used",
        "confidence_score",
        "validation_status",
        "review_required",
        "header_raw",
        "totals_raw",
        "page_text_raw",
    ]

    # Internal/system columns that are not useful in an export
    skip_cols = {"id", "batch_id", "tenant_id", "company_id", "source_file_id", "created_at"}

    existing_preferred = [c for c in preferred_order if c in df.columns]
    other_cols = [c for c in df.columns if c not in existing_preferred and c not in skip_cols]
    df = df[existing_preferred + other_cols]

    review_df = (
        df[df["review_required"] == True].copy()
        if "review_required" in df.columns
        else df.iloc[0:0].copy()
    )

    summary = {
        "total_rows": [len(df)],
        "needs_review": [len(review_df)],
        "sum_net_amount": [float(df["net_amount"].fillna(0).sum()) if "net_amount" in df.columns else 0],
        "sum_vat_amount": [float(df["vat_amount"].fillna(0).sum()) if "vat_amount" in df.columns else 0],
        "sum_total_amount": [float(df["total_amount"].fillna(0).sum()) if "total_amount" in df.columns else 0],
        "avg_confidence": [float(df["confidence_score"].fillna(0).mean()) if "confidence_score" in df.columns else 0],
        "distinct_source_files": [int(df["source_filename"].fillna("").replace("", pd.NA).dropna().nunique()) if "source_filename" in df.columns else 0],
    }
    summary_df = pd.DataFrame(summary)

    evidence_cols = [c for c in ["source_filename", "page_no", "invoice_number", "description", "line_items_raw", "header_raw", "totals_raw"] if c in df.columns]
    evidence_df = df[evidence_cols].copy() if evidence_cols else pd.DataFrame()

    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        review_df.to_excel(writer, index=False, sheet_name="Needs Review")
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
        evidence_df.to_excel(writer, index=False, sheet_name="Evidence")

    out.seek(0)
    return out

from __future__ import annotations
from io import BytesIO
import pandas as pd

def workbook_from_rows(rows: list[dict]) -> BytesIO:
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=[
            "page_no","supplier_name","invoice_number","invoice_date","description",
            "net_amount","vat_amount","total_amount","currency","tax_code",
            "method_used","confidence_score","validation_status","review_required"
        ])

    review_df = df[df["review_required"] == True].copy() if "review_required" in df.columns else df.iloc[0:0].copy()

    summary = {
        "total_rows": [len(df)],
        "needs_review": [len(review_df)],
        "sum_net_amount": [float(df["net_amount"].fillna(0).sum()) if "net_amount" in df.columns else 0],
        "sum_vat_amount": [float(df["vat_amount"].fillna(0).sum()) if "vat_amount" in df.columns else 0],
        "sum_total_amount": [float(df["total_amount"].fillna(0).sum()) if "total_amount" in df.columns else 0],
        "avg_confidence": [float(df["confidence_score"].fillna(0).mean()) if "confidence_score" in df.columns else 0],
    }
    summary_df = pd.DataFrame(summary)

    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        review_df.to_excel(writer, index=False, sheet_name="Needs Review")
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
    out.seek(0)
    return out

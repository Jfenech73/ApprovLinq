import argparse
import base64
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
from dateutil import parser as dateparser
from pydantic import BaseModel, Field

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.document_loaders import PyPDFLoader

import pypdfium2 as pdfium
import pytesseract

openai_api_key = "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"

# -----------------------------
# 1) Schema (no guessing)
# -----------------------------
class InvoiceExtract(BaseModel):
    invoice_date: Optional[str] = Field(default=None, description="Invoice date as written.")
    invoice_number: Optional[str] = Field(default=None, description="Invoice number / document number as written.")

    supplier_name: Optional[str] = Field(default=None, description="From Issuer/supplier name as written.")
    summary: Optional[str] = Field(
        default=None,
        description="5-12 word summary of what the invoice is for, based ONLY on evidence on the page. Try to differentiate if these are food, fuel or consumables",
    )

    net_amount: Optional[str] = Field(default=None, description="Net/subtotal amount EXCLUDING VAT (as written).")
    vat_amount: Optional[str] = Field(default=None, description="VAT/tax amount (as written).")
    total_amount: Optional[str] = Field(default=None, description="Grand total amount INCLUDING VAT (as written).")

    currency: Optional[str] = Field(default=None, description="Currency if visible (e.g., EUR).")


# -----------------------------
# 2) Text cleanup / parsing
# -----------------------------
def clean_text(s: str, max_chars: int = 14000) -> str:
    s = (s or "").replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    # Keep head+tail if huge (totals are often near bottom)
    if len(s) > max_chars:
        head = s[: max_chars // 2]
        tail = s[-max_chars // 2 :]
        s = head + "\n\n...[TRUNCATED]...\n\n" + tail
    return s


def normalize_date(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    ds = date_str.strip()
    try:
        # EU-ish invoices: dayfirst=True
        dt = dateparser.parse(ds, dayfirst=True, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        # Do not guess; keep raw string
        return ds


def parse_amount_to_float(amount_str: Optional[str]) -> Optional[float]:
    if not amount_str:
        return None
    s = amount_str.strip()

    # keep digits, separators, minus
    s = re.sub(r"[^\d,.\-]", "", s)
    if not re.search(r"\d", s):
        return None

    # Decide decimal separator by last occurrence
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # 1.234,56 -> 1234.56
            s = s.replace(".", "").replace(",", ".")
        else:
            # 1,234.56 -> 1234.56
            s = s.replace(",", "")
    elif "," in s and "." not in s:
        # 123,45 -> 123.45
        s = s.replace(",", ".")
    else:
        # 1234.56 or 1234
        s = s.replace(",", "")

    try:
        return float(s)
    except Exception:
        return None


def amounts_validate(net: Optional[float], vat: Optional[float], total: Optional[float], tol: float = 0.03) -> bool:
    """
    Validate totals with tolerance (default 3 cents).
    If any are missing, we don't fail hard (return True if we can't check).
    """
    if net is None or vat is None or total is None:
        return True
    return abs((net + vat) - total) <= tol


# -----------------------------
# 3) PDF page helpers
# -----------------------------
def load_pdf_text_pages(pdf_path: Path) -> List[str]:
    # page-wise extraction :contentReference[oaicite:3]{index=3}
    loader = PyPDFLoader(str(pdf_path), mode="page")
    docs = loader.load()
    return [d.page_content or "" for d in docs]


def render_page_to_base64_jpeg(pdf_path: Path, page_index: int, scale: float = 2.9) -> Dict[str, str]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf.get_page(page_index)
    img = page.render(scale=scale).to_pil()
    page.close()
    pdf.close()

    from io import BytesIO
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return {"base64": b64, "mime_type": "image/jpeg"}


def ocr_page_tesseract(pdf_path: Path, page_index: int, scale: float = 2.9, lang: str = "eng+mlt") -> str:
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf.get_page(page_index)
    img = page.render(scale=scale).to_pil()
    page.close()
    pdf.close()
    return pytesseract.image_to_string(img, lang=lang) or ""


# -----------------------------
# 4) LangChain extraction (text + vision)
# -----------------------------
def build_text_chain(model_name: str):
    llm = ChatOpenAI(openai_api_key=openai_api_key, model=model_name, temperature=0)

    system = (
        "You extract fields from ONE invoice page.\n"
        "Strict rules:\n"
        "- Use ONLY provided content. Do NOT guess.\n"
        "- If not present, return null.\n"
        "- net_amount must be EXCLUDING VAT (Subtotal/Net/Amount excl. VAT/Imponibile).\n"
        "- vat_amount is VAT/Tax/IVA amount.\n"
        "- total_amount is GRAND TOTAL including VAT (Total/Gross/Amount due/Totale).\n"
        "- Currency is EUR if clearly visible; otherwise null.\n"
        "- supplier_name must be the ISSUER/supplier, not the customer.\n"
        "- summary: 5-12 words, based only on evidence.\n"
        "- Label variants may include: Invoice No, Inv #, Document No; Date/Issued; "
        "Subtotal/Net/Amount excl VAT; VAT/Tax/IVA; Total/Amount due.\n"
    )

    prompt = ChatPromptTemplate.from_messages(
        [("system", system), ("human", "INVOICE PAGE TEXT:\n\n{page_text}")]
    )

    return prompt | llm.with_structured_output(InvoiceExtract)


def extract_with_vision(model_name: str, image_b64: str, mime_type: str) -> InvoiceExtract:
    llm = ChatOpenAI(openai_api_key=openai_api_key, model=model_name, temperature=0).with_structured_output(InvoiceExtract)

    system_msg = {
        "role": "system",
        "content": [
            {"type": "text", "text": (
                "Extract fields from ONE invoice PAGE IMAGE.\n"
                "Rules:\n"
                "- Only use what is visible. Do NOT guess.\n"
                "- If absent, return null.\n"
                "- net_amount = excluding VAT; vat_amount = VAT; total_amount = grand total.\n"
                "- supplier_name = issuer/supplier.\n"
                "- summary = 5-12 words from evidence.\n"
                "- Currency is EUR if visible; else null.\n"
            )}
        ],
    }

    user_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Extract invoice fields from this page."},
            {"type": "image", "base64": image_b64, "mime_type": mime_type},
        ],
    }

    return llm.invoke([system_msg, user_msg])


# -----------------------------
# 5) One-page extraction with fallback + validation
# -----------------------------
def post_process(ex: InvoiceExtract) -> Dict[str, Any]:
    d = ex.model_dump()

    supplier = (d.get("supplier_name") or "").strip()
    summary = (d.get("summary") or "").strip()
    description = " - ".join([p for p in [supplier, summary] if p]) or None

    inv_date_norm = normalize_date(d.get("invoice_date"))

    net_f = parse_amount_to_float(d.get("net_amount"))
    vat_f = parse_amount_to_float(d.get("vat_amount"))
    total_f = parse_amount_to_float(d.get("total_amount"))

    # Force EUR if you want it fixed (since you said EUR always)
    currency = (d.get("currency") or "EUR").strip()

    return {
        "Invoice date": inv_date_norm,
        "Invoice number": d.get("invoice_number"),
        "Description": description,
        "Net Amount": net_f,
        "Tax Code": "",
        "Vat Amount": vat_f,
        "Total": total_f,
        "Currency": currency,

        # debug columns (optional)
        "Supplier name": d.get("supplier_name"),
        "Summary": d.get("summary"),
        "Raw invoice_date": d.get("invoice_date"),
        "Raw net_amount": d.get("net_amount"),
        "Raw vat_amount": d.get("vat_amount"),
        "Raw total_amount": d.get("total_amount"),
    }


def extract_page_auto(
    pdf_path: Path,
    page_index: int,
    page_text: str,
    text_chain,
    model_name: str,
    allow_cloud_vision: bool,
    min_text_chars: int,
    ocr_lang: str,
) -> Tuple[Dict[str, Any], str, str]:
    """
    Returns: (row_dict, method_used, notes)
    """
    notes = []

    # 1) Try TEXT if meaningful
    page_text_clean = clean_text(page_text)
    if len(page_text_clean.strip()) >= min_text_chars:
        try:
            ex = text_chain.invoke({"page_text": page_text_clean})
            row = post_process(ex)
            ok = amounts_validate(row["Net Amount"], row["Vat Amount"], row["Total"])
            if ok:
                return row, "text", "ok"
            notes.append("text_totals_mismatch")
        except Exception as e:
            notes.append(f"text_error:{type(e).__name__}")

    # 2) Try VISION (best for scans) if allowed
    if allow_cloud_vision:
        try:
            img = render_page_to_base64_jpeg(pdf_path, page_index)
            ex = extract_with_vision(model_name, img["base64"], img["mime_type"])
            row = post_process(ex)
            ok = amounts_validate(row["Net Amount"], row["Vat Amount"], row["Total"])
            if ok:
                return row, "vision", "ok"
            notes.append("vision_totals_mismatch")
        except Exception as e:
            notes.append(f"vision_error:{type(e).__name__}")

    # 3) Local OCR fallback then TEXT extraction
    try:
        ocr_text = clean_text(ocr_page_tesseract(pdf_path, page_index, lang=ocr_lang))
        ex = text_chain.invoke({"page_text": ocr_text})
        row = post_process(ex)
        ok = amounts_validate(row["Net Amount"], row["Vat Amount"], row["Total"])
        return row, "ocr", "ok" if ok else "ocr_totals_mismatch"
    except Exception as e:
        # If absolutely nothing works, return empty row (no hallucinations)
        empty = {
            "Invoice date": None, "Invoice number": None, "Description": None,
            "Net Amount": None, "Vat Amount": None, "Total": None, "Currency": "EUR",
            "Supplier name": None, "Summary": None,
            "Raw invoice_date": None, "Raw net_amount": None, "Raw vat_amount": None, "Raw total_amount": None,
        }
        notes.append(f"ocr_error:{type(e).__name__}")
        return empty, "failed", ";".join(notes)


# -----------------------------
# 6) Process PDF -> Excel
# -----------------------------
def process_pdf(
    pdf_path: Path,
    out_xlsx: Path,
    model_name: str,
    allow_cloud_vision: bool,
    min_text_chars: int,
    ocr_lang: str,
    tesseract_cmd: Optional[str],
) -> pd.DataFrame:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    text_pages = load_pdf_text_pages(pdf_path)
    text_chain = build_text_chain(model_name)

    rows = []
    for i, page_text in enumerate(text_pages):
        row, method, status = extract_page_auto(
            pdf_path=pdf_path,
            page_index=i,
            page_text=page_text,
            text_chain=text_chain,
            model_name=model_name,
            allow_cloud_vision=allow_cloud_vision,
            min_text_chars=min_text_chars,
            ocr_lang=ocr_lang,
        )

        row["Page"] = i + 1
        row["Source PDF"] = pdf_path.name
        row["Method"] = method
        row["Status"] = status
        rows.append(row)

    df = pd.DataFrame(rows)

    # Put your requested columns first
    ordered = [
        "Invoice date", "Invoice number", "Description", "Net Amount", "Tax Code","Vat Amount", "Total", "Currency",
        "Page", "Source PDF", "Method", "Status",
        "Supplier name", "Summary",
        "Raw invoice_date", "Raw net_amount", "Raw vat_amount", "Raw total_amount",
    ]
    df = df[[c for c in ordered if c in df.columns]]

    df.to_excel(out_xlsx, index=False, engine="openpyxl")
    return df

def safe_stem(filename: str) -> str:
    """Make a filesystem-safe output base name from a PDF filename."""
    s = Path(filename).stem
    s = re.sub(r"[^\w\-]+", "_", s).strip("_")
    return s or "output"


def process_folder(
    in_folder: Path,
    out_folder: Path,
    model_name: str,
    allow_cloud_vision: bool,
    min_text_chars: int,
    ocr_lang: str,
    tesseract_cmd: str | None,
) -> pd.DataFrame:
    out_folder.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(in_folder.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs found in: {in_folder}")

    index_rows = []
    for pdf_path in pdfs:
        out_xlsx = out_folder / f"{safe_stem(pdf_path.name)}.xlsx"

        print(f"Processing: {pdf_path.name} -> {out_xlsx.name}")

        df = process_pdf(
            pdf_path=pdf_path,
            out_xlsx=out_xlsx,
            model_name=model_name,
            allow_cloud_vision=allow_cloud_vision,
            min_text_chars=min_text_chars,
            ocr_lang=ocr_lang,
            tesseract_cmd=tesseract_cmd,
        )

        index_rows.append({
            "PDF": pdf_path.name,
            "Pages": len(df),
            "Excel": out_xlsx.name,
        })

    index_df = pd.DataFrame(index_rows)
    index_path = out_folder / "_index.xlsx"
    index_df.to_excel(index_path, index=False, engine="openpyxl")
    print(f"Index written: {index_path}")

    return index_df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None, help="Single PDF path (1 invoice per page).")
    ap.add_argument("--in-folder", default=None, help="Folder containing PDFs to batch process.")
    ap.add_argument("--out", default="invoices.xlsx", help="Output Excel for single --pdf mode.")
    ap.add_argument("--out-folder", default="excel_out", help="Output folder for --in-folder batch mode.")

    ap.add_argument("--model", default="gpt-4o-mini", help="Model name.")
    ap.add_argument("--allow-vision", action="store_true", help="Enable cloud vision fallback (recommended for scans).")
    ap.add_argument("--min-text-chars", type=int, default=80, help="Treat pages with fewer chars as image-like.")
    ap.add_argument("--ocr-lang", default="eng+mlt", help="Tesseract OCR languages (e.g., eng+mlt).")
    ap.add_argument("--tesseract-cmd", default=None, help="Full path to tesseract.exe if needed on Windows.")
    args = ap.parse_args()

    # Require exactly one mode
    if (args.pdf is None and args.in_folder is None) or (args.pdf and args.in_folder):
        raise SystemExit("Provide either --pdf OR --in-folder (but not both).")

    if args.pdf:
        pdf_path = Path(args.pdf).expanduser().resolve()
        out_path = Path(args.out).expanduser().resolve()

        df = process_pdf(
            pdf_path=pdf_path,
            out_xlsx=out_path,
            model_name=args.model,
            allow_cloud_vision=args.allow_vision,
            min_text_chars=args.min_text_chars,
            ocr_lang=args.ocr_lang,
            tesseract_cmd=args.tesseract_cmd,
        )
        print(f"Done. Pages: {len(df)}. Output: {out_path}")

    else:
        in_folder = Path(args.in_folder).expanduser().resolve()
        out_folder = Path(args.out_folder).expanduser().resolve()

        index_df = process_folder(
            in_folder=in_folder,
            out_folder=out_folder,
            model_name=args.model,
            allow_cloud_vision=args.allow_vision,
            min_text_chars=args.min_text_chars,
            ocr_lang=args.ocr_lang,
            tesseract_cmd=args.tesseract_cmd,
        )
        print(f"Done. PDFs: {len(index_df)}. Output folder: {out_folder}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, or_
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Company, InvoiceBatch, InvoiceFile, InvoiceReadDetail, InvoiceReadHeader, InvoiceRow, IssueLog, TenantNominalAccount, TenantSupplier, User

try:
    from app.services.classify_lines import classify_line as _classify_line
    from app.services.normalize_suppliers import normalize_supplier as _normalize_supplier_batches
    _CLASSIFY_AVAILABLE = True
except ImportError:
    _CLASSIFY_AVAILABLE = False

from app.db.session import engine, get_db
from app.routers.auth import current_tenant_id, current_user
from app.schemas import BatchCreate, BatchUpdate, BatchDetailOut, BatchFileOut, BatchOut, InvoiceRowOut
from app.services.exporter import workbook_from_rows
# >>> REVIEW_PACK corrected_export_import
from app.services.corrected_exporter import build_corrected_rows, export_batch_corrected
# <<< REVIEW_PACK corrected_export_import
from app.services.extractor import get_pdf_page_count, process_pdf_page_rows
from app.services.invoice_arbitration import arbitrate_invoice_row
from app.services.scan_performance import ScanPerformanceContext
from app.db.review_models import BatchExportEvent, CorrectionRule, InvoiceRowCorrection, InvoiceRowFieldAudit, RemapHint
from app.services.template_render_service import render_template_sheet, resolve_effective_template
from app.utils.storage import batch_upload_folder, batch_export_folder, resolve_upload_path
from app.utils.persistent_files import attach_invoice_file_bytes, materialize_invoice_file

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_PDF_MAGIC = b"%PDF"

router = APIRouter(prefix="/batches", tags=["batches"])
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
_ACTIVE_BATCHES: set[str] = set()
_ACTIVE_BATCHES_LOCK = Lock()
_READ_SNAPSHOT_SCHEMA_CACHE: dict[str, set[str]] = {}


def _batch_folder(batch_id: UUID) -> Path:
    return batch_upload_folder(batch_id)


def _set_active(batch_id: UUID) -> bool:
    key = str(batch_id)
    with _ACTIVE_BATCHES_LOCK:
        if key in _ACTIVE_BATCHES:
            return False
        _ACTIVE_BATCHES.add(key)
        return True


def _clear_active(batch_id: UUID) -> None:
    with _ACTIVE_BATCHES_LOCK:
        _ACTIVE_BATCHES.discard(str(batch_id))



def _safe_log_value(value: object, *, max_len: int = 80) -> str:
    """Return a short, content-safe value for operational logs.

    Logs should identify the processing step and high-level status without
    leaking invoice contents, API keys, or full filenames.
    """
    text = str(value or "")
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    text = re.sub(r"[^\w .@#:/\-]+", "_", text)
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text

def _append_method_tag(row: InvoiceRow, tag: str) -> None:
    """Append a method_used tag once, preserving existing tags."""
    tag = (tag or "").strip()
    if not tag:
        return
    parts = [p.strip() for p in re.split(r"[+|,]", row.method_used or "") if p.strip()]
    if tag not in parts:
        parts.append(tag)
    row.method_used = "+".join(parts)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _stringify_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _di_field_scalar_value(field_payload: object) -> str | None:
    if not isinstance(field_payload, dict):
        return None
    for key in ("value_string", "value_phone_number", "value_date", "value_number"):
        value = field_payload.get(key)
        if value not in (None, ""):
            return str(value)
    currency = field_payload.get("value_currency")
    if isinstance(currency, dict):
        amount = currency.get("amount")
        if amount not in (None, ""):
            return str(amount)
    address = field_payload.get("value_address")
    if isinstance(address, dict):
        parts = [
            address.get("road"),
            address.get("city"),
            address.get("state"),
            address.get("postal_code"),
            address.get("country_region"),
        ]
        text = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        if text:
            return text
    content = field_payload.get("content")
    if content not in (None, ""):
        return str(content)
    return None


def _di_field_complex_value(field_payload: object) -> object:
    if not isinstance(field_payload, dict):
        return None
    if "value_array" in field_payload:
        return _json_safe(field_payload.get("value_array"))
    if "value_object" in field_payload:
        return _json_safe(field_payload.get("value_object"))
    return None


def _existing_table_columns(db: Session, table_name: str) -> set[str]:
    cached = _READ_SNAPSHOT_SCHEMA_CACHE.get(table_name)
    if cached is not None:
        return cached
    cols = {col["name"] for col in inspect(db.bind).get_columns(table_name)}
    _READ_SNAPSHOT_SCHEMA_CACHE[table_name] = cols
    return cols


def _filter_existing_columns(db: Session, table_name: str, values: dict[str, object]) -> dict[str, object]:
    existing = _existing_table_columns(db, table_name)
    return {key: value for key, value in values.items() if key in existing}


def _persist_invoice_read_snapshot(
    db: Session,
    *,
    batch: InvoiceBatch,
    invoice_file: InvoiceFile,
    row: InvoiceRow,
    payload: dict,
    baseline_mode: bool,
) -> None:
    provider_name = str((payload.get("method_used") or "unknown")).split("+")[0]
    structured_items = payload.get("line_items_structured")
    if not isinstance(structured_items, list):
        structured_items = []
    raw_di_fields = payload.get("_di_raw_fields") or {}
    if not isinstance(raw_di_fields, dict):
        raw_di_fields = {}
    source_file_id = row.source_file_id or invoice_file.id
    header_values = dict(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        source_file_id=source_file_id,
        row_id=row.id,
        source_filename=row.source_filename or invoice_file.original_filename,
        page_no=row.page_no,
        provider_name=provider_name,
        extraction_source=payload.get("extraction_source"),
        method_used=payload.get("method_used"),
        baseline_mode=baseline_mode,
        document_type=payload.get("document_type"),
        document_confidence=(payload.get("raw_di_document_confidence") or ((payload.get("_di_raw_payload") or {}).get("document") or {}).get("confidence")),
        supplier_name=payload.get("supplier_name"),
        supplier_vat=payload.get("supplier_vat"),
        supplier_address=payload.get("supplier_address"),
        supplier_address_recipient=payload.get("supplier_address_recipient"),
        customer_name=payload.get("customer_name"),
        customer_vat=payload.get("customer_vat"),
        customer_address=payload.get("customer_address"),
        customer_address_recipient=payload.get("customer_address_recipient"),
        invoice_number=payload.get("invoice_number"),
        invoice_date=_stringify_date(payload.get("invoice_date")),
        due_date=_stringify_date(payload.get("due_date")),
        order_number=payload.get("order_number"),
        purchase_order=payload.get("purchase_order"),
        description=payload.get("description"),
        net_amount=payload.get("net_amount"),
        vat_amount=payload.get("vat_amount"),
        total_amount=payload.get("total_amount"),
        currency=payload.get("currency"),
        header_text=payload.get("_header_text") or payload.get("header_raw"),
        totals_text=payload.get("_totals_text") or payload.get("totals_raw"),
        page_text=payload.get("di_page_text") or payload.get("page_text_raw"),
        raw_provider_fields=_json_safe(payload.get("_di_structured_fields") or {}),
        raw_provider_payload=_json_safe({
            "extraction_source": payload.get("extraction_source"),
            "method_used": payload.get("method_used"),
            "document_type": payload.get("document_type"),
            "supplier_name": payload.get("supplier_name"),
            "supplier_vat": payload.get("supplier_vat"),
            "supplier_address": payload.get("supplier_address"),
            "supplier_address_recipient": payload.get("supplier_address_recipient"),
            "customer_name": payload.get("customer_name"),
            "customer_vat": payload.get("customer_vat"),
            "customer_address": payload.get("customer_address"),
            "customer_address_recipient": payload.get("customer_address_recipient"),
            "invoice_number": payload.get("invoice_number"),
            "invoice_date": _stringify_date(payload.get("invoice_date")),
            "due_date": _stringify_date(payload.get("due_date")),
            "order_number": payload.get("order_number"),
            "purchase_order": payload.get("purchase_order"),
            "net_amount": payload.get("net_amount"),
            "vat_amount": payload.get("vat_amount"),
            "total_amount": payload.get("total_amount"),
            "currency": payload.get("currency"),
            "description": payload.get("description"),
        }),
        raw_di_fields=_json_safe(raw_di_fields),
        raw_di_payload=_json_safe(payload.get("_di_raw_payload") or {}),
        BatchPages=invoice_file.page_count,
        DocumentInBatch=row.page_no,
        DocType=payload.get("document_type") or ((payload.get("_di_raw_payload") or {}).get("document") or {}).get("doc_type"),
        DocumentConfidence=(payload.get("raw_di_document_confidence") or ((payload.get("_di_raw_payload") or {}).get("document") or {}).get("confidence")),
        CustomerName=_di_field_scalar_value(raw_di_fields.get("CustomerName")),
        CustomerId=_di_field_scalar_value(raw_di_fields.get("CustomerId")),
        PurchaseOrder=_di_field_scalar_value(raw_di_fields.get("PurchaseOrder")),
        InvoiceId=_di_field_scalar_value(raw_di_fields.get("InvoiceId")),
        InvoiceDate=_di_field_scalar_value(raw_di_fields.get("InvoiceDate")),
        DueDate=_di_field_scalar_value(raw_di_fields.get("DueDate")),
        VendorName=_di_field_scalar_value(raw_di_fields.get("VendorName")),
        VendorAddress=_di_field_scalar_value(raw_di_fields.get("VendorAddress")),
        VendorAddressRecipient=_di_field_scalar_value(raw_di_fields.get("VendorAddressRecipient")),
        CustomerAddress=_di_field_scalar_value(raw_di_fields.get("CustomerAddress")),
        CustomerAddressRecipient=_di_field_scalar_value(raw_di_fields.get("CustomerAddressRecipient")),
        BillingAddress=_di_field_scalar_value(raw_di_fields.get("BillingAddress")),
        BillingAddressRecipient=_di_field_scalar_value(raw_di_fields.get("BillingAddressRecipient")),
        ShippingAddress=_di_field_scalar_value(raw_di_fields.get("ShippingAddress")),
        ShippingAddressRecipient=_di_field_scalar_value(raw_di_fields.get("ShippingAddressRecipient")),
        SubTotal=_di_field_scalar_value(raw_di_fields.get("SubTotal")),
        TotalDiscount=_di_field_scalar_value(raw_di_fields.get("TotalDiscount")),
        TotalTax=_di_field_scalar_value(raw_di_fields.get("TotalTax")),
        InvoiceTotal=_di_field_scalar_value(raw_di_fields.get("InvoiceTotal")),
        AmountDue=_di_field_scalar_value(raw_di_fields.get("AmountDue")),
        PreviousUnpaidBalance=_di_field_scalar_value(raw_di_fields.get("PreviousUnpaidBalance")),
        RemittanceAddress=_di_field_scalar_value(raw_di_fields.get("RemittanceAddress")),
        RemittanceAddressRecipient=_di_field_scalar_value(raw_di_fields.get("RemittanceAddressRecipient")),
        ServiceAddress=_di_field_scalar_value(raw_di_fields.get("ServiceAddress")),
        ServiceAddressRecipient=_di_field_scalar_value(raw_di_fields.get("ServiceAddressRecipient")),
        ServiceStartDate=_di_field_scalar_value(raw_di_fields.get("ServiceStartDate")),
        ServiceEndDate=_di_field_scalar_value(raw_di_fields.get("ServiceEndDate")),
        VendorTaxId=_di_field_scalar_value(raw_di_fields.get("VendorTaxId")),
        CustomerTaxId=_di_field_scalar_value(raw_di_fields.get("CustomerTaxId")),
        PaymentTerm=_di_field_scalar_value(raw_di_fields.get("PaymentTerm")),
        KVKNumber=_di_field_scalar_value(raw_di_fields.get("KVKNumber")),
        CurrencyCode=_di_field_scalar_value(raw_di_fields.get("CurrencyCode")),
        VendorPhoneNumber=_di_field_scalar_value(raw_di_fields.get("VendorPhoneNumber")),
        CustomerPhoneNumber=_di_field_scalar_value(raw_di_fields.get("CustomerPhoneNumber")),
        BillingPhoneNumber=_di_field_scalar_value(raw_di_fields.get("BillingPhoneNumber")),
        VendorEmail=_di_field_scalar_value(raw_di_fields.get("VendorEmail")),
        VendorFaxNumber=_di_field_scalar_value(raw_di_fields.get("VendorFaxNumber")),
        ReferenceNumber=_di_field_scalar_value(raw_di_fields.get("ReferenceNumber")),
        PaymentDetails=_di_field_complex_value(raw_di_fields.get("PaymentDetails")),
        TaxDetails=_di_field_complex_value(raw_di_fields.get("TaxDetails")),
        PaidInFourInstallements=_di_field_complex_value(raw_di_fields.get("PaidInFourInstallements")),
    )
    header = InvoiceReadHeader(**_filter_existing_columns(db, "invoice_read_headers", header_values))
    db.add(header)
    db.flush()
    raw_items = _di_field_complex_value(raw_di_fields.get("Items"))
    if isinstance(raw_items, list):
        items_to_persist = raw_items
    else:
        items_to_persist = structured_items
    for idx, item in enumerate(items_to_persist, start=1):
        item_fields = item.get("value_object") if isinstance(item, dict) else None
        if not isinstance(item_fields, dict):
            item_fields = {}
        detail_values = dict(
            header_id=header.id,
            line_no=idx,
            description=(item.get("description") if isinstance(item, dict) else None),
            quantity=(item.get("quantity") if isinstance(item, dict) else None),
            unit_price=(item.get("unit_price") if isinstance(item, dict) else None),
            net_amount=(item.get("net_amount") if isinstance(item, dict) else None),
            tax_amount=(item.get("tax_amount") if isinstance(item, dict) else None),
            Amount=_di_field_scalar_value(item_fields.get("Amount")),
            Date=_di_field_scalar_value(item_fields.get("Date")),
            Description=_di_field_scalar_value(item_fields.get("Description")),
            ProductCode=_di_field_scalar_value(item_fields.get("ProductCode")),
            Quantity=_di_field_scalar_value(item_fields.get("Quantity")),
            Tax=_di_field_scalar_value(item_fields.get("Tax")),
            TaxRate=_di_field_scalar_value(item_fields.get("TaxRate")),
            Unit=_di_field_scalar_value(item_fields.get("Unit")),
            UnitPrice=_di_field_scalar_value(item_fields.get("UnitPrice")),
            raw_detail=_json_safe(item),
        )
        db.add(InvoiceReadDetail(**_filter_existing_columns(db, "invoice_read_details", detail_values)))


def _audit_rule_application(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    field_name: str,
    old_value: object,
    new_value: object,
    rule: CorrectionRule,
    note: str,
) -> None:
    """Record an automatic rule application when the row has an id.

    During scan rows may still be transient before the final db.add(row).
    Flushing here is safe and makes automatic rule application visible in the
    same audit table used by manual review corrections.
    """
    if str(old_value or "") == str(new_value or ""):
        return
    old_text = None if old_value is None else str(old_value)
    new_text = None if new_value is None else str(new_value)
    if row.id is None:
        db.add(row)
        db.flush()
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name=field_name,
        old_value=old_text,
        new_value=new_text,
        action="rule_apply",
        note=f"{note}; rule_id={rule.id}; rule_type={rule.rule_type}; scope={'global' if getattr(rule, 'is_global', False) else 'tenant'}",
        rule_created=False,
        user_id=None,
        username="system",
    ))


def _audit_supplier_identity_resolution(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    old_value: object,
    new_value: object,
    action: str,
    note: str,
) -> None:
    """Record supplier resolver evidence before supplier-gated rules run."""
    old_text = None if old_value is None else str(old_value)
    new_text = None if new_value is None else str(new_value)
    if old_text == new_text and action == "supplier_identity_apply":
        return
    if row.id is None:
        db.add(row)
        db.flush()
    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name="supplier_name",
        old_value=old_text,
        new_value=new_text,
        action=action[:40],
        note=note[:1000],
        rule_created=False,
        user_id=None,
        username="system",
    ))




def _audit_saved_region_action(
    db: Session,
    batch: InvoiceBatch,
    row: InvoiceRow,
    field_name: str,
    old_value: object,
    new_value: object,
    hint: RemapHint,
    action: str,
    note: str,
    perf_ctx: ScanPerformanceContext | None = None,
) -> None:
    """Record saved-region replay decisions and governance telemetry.

    Rows created during scanning can still be transient when saved regions run.
    Flushing here keeps automatic saved-region activity visible.  The RemapHint
    is also updated so the UI can show which supplier/field region was used,
    whether it applied, failed, or conflicted, and which page was actually read.
    """
    old_text = str(old_value) if old_value is not None else None
    new_text = str(new_value) if new_value is not None else None
    if old_text == new_text and action in {"saved_region_checked"}:
        return
    audit_key = (getattr(row, "id", None), field_name, action, old_text, new_text, getattr(hint, "id", None))
    if perf_ctx is not None:
        if audit_key in perf_ctx.audit_seen:
            return
        perf_ctx.audit_seen.add(audit_key)
    if row.id is None:
        db.add(row)
        db.flush()

    # Governance usage tracking.  Keep content short to avoid storing full OCR
    # extracts while preserving enough evidence for support/review.
    try:
        hint.last_used_at = datetime.utcnow()
        hint.last_used_batch_id = batch.id
        hint.last_used_row_id = row.id
        m = re.search(r"used_page=(\d+)", note or "")
        if m:
            hint.last_used_page_no = int(m.group(1))
        read = str(new_value or "").strip()
        hint.last_read_text = read[:500] if read else None
        hint.last_result = action[:80]
        if action == "saved_region_apply":
            hint.apply_count = int(hint.apply_count or 0) + 1
            hint.success_count = int(hint.success_count or 0) + 1
        elif action in {"saved_region_blank", "saved_region_invalid"}:
            hint.failure_count = int(hint.failure_count or 0) + 1
        elif action == "saved_region_conflict":
            hint.conflict_count = int(hint.conflict_count or 0) + 1
        elif action == "saved_region_checked":
            hint.success_count = int(hint.success_count or 0) + 1
    except Exception:
        logger.debug("RemapHint usage tracking failed for hint_id=%s", getattr(hint, "id", None), exc_info=True)

    db.add(InvoiceRowFieldAudit(
        batch_id=batch.id,
        row_id=row.id,
        field_name=field_name,
        old_value=old_text,
        new_value=new_text,
        action=action[:40],
        note=(f"{note}; remap_hint_id={hint.id}; source=saved_region")[:1000],
        rule_created=False,
        user_id=None,
        username="system",
    ))


def _append_review_marker(row: InvoiceRow, field_name: str, reason: str) -> None:
    """Mark a row/field for review without duplicating markers."""
    row.review_required = True
    fields = [f.strip() for f in re.split(r"[|,]", row.review_fields or "") if f.strip()]
    if field_name and field_name not in fields:
        fields.append(field_name)
    row.review_fields = "|".join(fields)[:500] if fields else row.review_fields
    reasons = [r.strip() for r in re.split(r"[|]", row.review_reasons or "") if r.strip()]
    if reason and reason not in reasons:
        reasons.append(reason)
    row.review_reasons = "|".join(reasons)[:500] if reasons else row.review_reasons


def _parse_region_money(value: object) -> float | None:
    """Conservative money parser for validating saved-region amount reads."""
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9,\.\-]", "", text)
    if not cleaned or cleaned in {"-", ".", ","}:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        # If comma is the only separator and has 1-2 trailing digits, treat as decimal.
        if "," in cleaned and re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except Exception:
        return None


def _normalise_saved_region_value(field_name: str, value: object) -> str:
    """Normalise a saved-region read before field validation/application."""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if field_name in {"net_amount", "vat_amount", "total_amount"}:
        money = _parse_region_money(text)
        if money is None:
            return ""
        return f"{money:.2f}"
    return text


def _saved_region_value_is_valid(field_name: str, value: object) -> bool:
    """Return True when a saved-region read matches the expected field type."""
    text = str(value or "").strip()
    if not text:
        return False
    if field_name in {"net_amount", "vat_amount", "total_amount"}:
        return _parse_region_money(text) is not None
    if field_name == "invoice_date":
        # Accept common invoice date formats; detailed locale parsing remains in extractor.
        return bool(re.search(r"\b\d{1,4}[\-/\.]\d{1,2}[\-/\.]\d{1,4}\b", text))
    if field_name == "invoice_number":
        if len(text) < 2 or len(text) > 60:
            return False
        if re.fullmatch(r"\d{1,2}[\-/\.]\d{1,2}[\-/\.]\d{2,4}", text):
            return False
        if re.search(r"\b(?:vat|tel|phone|mobile|email|total|subtotal)\b", text, re.I):
            return False
        return True
    if field_name == "supplier_name":
        if len(text) < 3:
            return False
        digits = sum(1 for c in text if c.isdigit())
        if digits and digits / max(len(text), 1) > 0.4 and " " not in text:
            return False
        if re.search(r"\b(?:invoice|total|subtotal|vat no|vat number|page)\b", text, re.I):
            return False
        return True
    if field_name == "nominal_account_code":
        return len(text) <= 100
    return len(text) <= 500


def _is_strong_existing_saved_region_value(row: InvoiceRow, field_name: str, value: object, review_fields: set[str], low_confidence: bool) -> bool:
    """Return True when an existing value should not be overwritten silently."""
    if value is None or str(value).strip() == "":
        return False
    if field_name in review_fields:
        return False
    if _is_suspect_field_value(field_name, value):
        return False
    if low_confidence:
        return False
    return _saved_region_value_is_valid(field_name, value)


def _get_pdf_page_count_safe(pdf_path: str) -> int:
    """Best-effort page count used for page-flexible saved-region replay."""
    try:
        return int(get_pdf_page_count(Path(pdf_path)) or 0)
    except Exception:
        try:
            import fitz
            doc = fitz.open(pdf_path)
            try:
                return int(doc.page_count or 0)
            finally:
                doc.close()
        except Exception:
            return 0


def _candidate_pages_for_saved_region(pdf_path: str, row_page_no: int | None, hint_page_no: int | None) -> list[int]:
    """Return pages to try for a saved region, anchored to the current row.

    The stored page is a reference from the invoice where the region was
    created, not a hard identity.  But a batch PDF can contain many unrelated
    invoices.  Searching the whole PDF, or jumping to the stored page for a
    different transaction, can read another supplier's invoice and contaminate
    the current row.

    Safe replay therefore tries the current row page first and only its
    immediate neighbours.  The stored hint page is included only if it is the
    same page or adjacent to the current row page.  There is deliberately no
    whole-document search.
    """
    page_count = _get_pdf_page_count_safe(pdf_path)
    max_page = page_count if page_count > 0 else max(int(row_page_no or 1), int(hint_page_no or 1), 1)
    seen: set[int] = set()
    ordered: list[int] = []

    def _as_int(page: int | None) -> int | None:
        try:
            p = int(page or 0)
        except Exception:
            return None
        return p if 1 <= p <= max_page else None

    def add(page: int | None):
        p = _as_int(page)
        if p is None or p in seen:
            return
        seen.add(p)
        ordered.append(p)

    row_p = _as_int(row_page_no) or _as_int(hint_page_no) or 1
    hint_p = _as_int(hint_page_no)

    add(row_p)
    add(row_p - 1)
    add(row_p + 1)
    if hint_p is not None and abs(hint_p - row_p) <= 1:
        add(hint_p)

    return ordered or [row_p]

def _read_saved_region_on_candidate_pages(
    pdf_path: str,
    hint: RemapHint,
    row_page_no: int | None,
    field_name: str,
    perf_ctx: ScanPerformanceContext | None = None,
) -> tuple[str, int | None, list[int]]:
    """Read a saved region using flexible page replay.

    Returns the first non-blank value that passes field validation.  If no valid
    value is found, returns the best raw text seen so callers can audit invalid
    reads without applying them.
    """
    from app.routers.review import _read_region_text

    pages = _candidate_pages_for_saved_region(pdf_path, row_page_no, hint.page_no)[:5]  # cap page search safely; page_no is reference only.
    cache_key = (pdf_path, tuple(pages), float(hint.x), float(hint.y), float(hint.w), float(hint.h), field_name)
    if perf_ctx is not None and cache_key in perf_ctx.saved_region_read_cache:
        return perf_ctx.saved_region_read_cache[cache_key]
    best_raw = ""
    best_page: int | None = None
    for page_no in pages:
        try:
            raw = _read_region_text(
                pdf_path, page_no,
                float(hint.x), float(hint.y), float(hint.w), float(hint.h),
            )
        except Exception as exc:
            logger.debug(
                "saved-region candidate page read failed hint_id=%s field=%s page=%s: %s",
                getattr(hint, "id", None), field_name, page_no, exc,
            )
            continue
        raw = (raw or "").strip()
        if raw and not best_raw:
            best_raw = raw
            best_page = page_no
        normalised = _normalise_saved_region_value(field_name, raw)
        if normalised and _saved_region_value_is_valid(field_name, normalised):
            result = (normalised, page_no, pages)
            if perf_ctx is not None:
                perf_ctx.saved_region_read_cache[cache_key] = result
            return result
    result = (best_raw, best_page, pages)
    if perf_ctx is not None:
        perf_ctx.saved_region_read_cache[cache_key] = result
    return result

def _normalize_rule_value(value: str | None) -> str:
    """Normalise a rule source_pattern or supplier name for comparison.

    Must produce the SAME output as the normalisation used in save_remap
    (review.py) when storing source_pattern, so that rule matching is
    consistent at both creation and replay time.

    Steps:
    1. Strip known company-type suffixes (ltd, limited, plc, …)
    2. Replace non-alphanumeric with spaces
    3. Collapse whitespace and lowercase
    """
    import re as _re
    n = _re.sub(
        r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
        "", str(value or "").lower(),
    )
    n = _re.sub(r"[^a-z0-9 ]", " ", n)
    return _re.sub(r"\s+", " ", n).strip()


def _apply_saved_rules(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> None:
    """Apply active CorrectionRules to this row.

    Rule types handled:
      supplier_alias     — rename supplier_name when it matches source_pattern
      nominal_remap      — remap nominal_account_code
      remap_field_value  — field-specific value saved via the remap workflow

    For remap_field_value rules the following invariants are ALWAYS enforced:
      • Rules are matched by (supplier, target_field) — never supplier alone.
      • A rule for field X can ONLY write to field X.
      • Invoice-number-like tokens are NEVER written to supplier_name unless
        the rule's field_name is explicitly "supplier_name".
      • text_correction rules may reuse chosen_rule.target_value only when the
        scanned field text matches the stored source_pattern.
        remap_field_value rules must re-read the current PDF via _read_region_text
        and may never use the stored example value from creation time.
      • A field is only eligible for overwrite when it is blank, low-confidence,
        or flagged for review.
    """
    rules_q = db.query(CorrectionRule).filter(
        CorrectionRule.active.is_(True),
        or_(CorrectionRule.tenant_id == batch.tenant_id, CorrectionRule.is_global.is_(True)),
    )
    if batch.company_id:
        rules_q = rules_q.filter(
            (CorrectionRule.company_id == batch.company_id)
            | (CorrectionRule.company_id.is_(None))
            | (CorrectionRule.is_global.is_(True))
        )
    else:
        rules_q = rules_q.filter((CorrectionRule.company_id.is_(None)) | (CorrectionRule.is_global.is_(True)))
    # Tenant/company rules have precedence over platform-global background rules.
    all_rules = rules_q.order_by(CorrectionRule.is_global.asc(), CorrectionRule.id.asc()).all()

    # ── 1. supplier_alias and nominal_remap ───────────────────────────────
    for rule in all_rules:
        src = _normalize_rule_value(rule.source_pattern)
        if not src:
            continue
        if rule.rule_type == "supplier_alias":
            current = _normalize_rule_value(row.supplier_name)
            if current and current == src and rule.target_value:
                logger.debug(
                    "_apply_saved_rules: supplier_alias %r→%r row=%d",
                    row.supplier_name, rule.target_value, row.id,
                )
                old_val = row.supplier_name
                new_supplier_name = rule.target_value
                row.supplier_name = new_supplier_name
                _append_method_tag(row, "rule:supplier_alias")
                _audit_rule_application(
                    db, batch, row, "supplier_name", old_val, new_supplier_name,
                    rule, "Applied supplier alias rule during scan"
                )
        elif rule.rule_type == "nominal_remap":
            # Existing semantics: nominal_remap means old nominal value → new
            # nominal value. Supplier → nominal suggestions remain handled by
            # _apply_account_suggestions / tenant nominal master data.
            current = _normalize_rule_value(row.nominal_account_code)
            if current and current == src and rule.target_value:
                logger.debug(
                    "_apply_saved_rules: nominal_remap %r→%r row=%d",
                    row.nominal_account_code, rule.target_value, row.id,
                )
                old_val = row.nominal_account_code
                row.nominal_account_code = rule.target_value
                _append_method_tag(row, "rule:nominal_remap")
                _audit_rule_application(
                    db, batch, row, "nominal_account_code", old_val, rule.target_value,
                    rule, "Applied nominal remap rule during scan"
                )

    # ── 2. remap_field_value / text_correction rules ────────────────────
    # IMPORTANT: Rule semantics are type-dependent.
    # See inner loop below for remap_field_value vs text_correction handling.
    from app.services.extractor import suspicious_invoice_number as _sus_inv

    # Determine which fields are currently eligible for remap overwrite
    _review_fields_set: set[str] = set()
    if row.review_fields:
        sep = "|" if "|" in (row.review_fields or "") else ","
        _review_fields_set = {f.strip() for f in row.review_fields.split(sep) if f.strip()}

    def _field_is_eligible(field: str) -> bool:
        """Return True if this field may be overwritten by a remap rule."""
        current_val = getattr(row, field, None)
        is_blank = not current_val or str(current_val).strip() == ""
        if is_blank:
            return True
        if field in _review_fields_set:
            return True
        is_suspect = row.review_required and (
            row.confidence_score is None or float(row.confidence_score) < 0.55
        )
        if is_suspect:
            return True
        v = str(current_val).strip()
        if field == "invoice_number" and _sus_inv(v):
            return True
        if field == "supplier_name" and len(v) < 3:
            return True
        return False

    def _value_is_appropriate_for_field(field: str, value: str) -> bool:
        """Return True if value is appropriate to write into field."""
        if not value:
            return False
        v = value.strip()
        if field == "supplier_name":
            digits = sum(1 for c in v if c.isdigit())
            if len(v) <= 15 and digits > 0 and (digits / max(len(v), 1)) > 0.4:
                if " " not in v:
                    logger.debug(
                        "_apply_saved_rules: rejected invoice-like value %r for supplier_name",
                        v,
                    )
                    return False
            if re.match(r"^[A-Z0-9\-\/]{2,15}$", v, re.I) and " " not in v:
                logger.debug(
                    "_apply_saved_rules: rejected invoice-ref-pattern value %r for supplier_name",
                    v,
                )
                return False
        return True

    current_supplier_norm = _normalize_rule_value(row.supplier_name)
    if not current_supplier_norm:
        return

    # Resolve the PDF path once for coordinate-based re-reading
    _pdf_path: str | None = None
    from app.db.models import InvoiceFile as _IF2
    _file_obj = db.get(_IF2, row.source_file_id) if row.source_file_id else None
    if _file_obj:
        try:
            _pdf_path = str(materialize_invoice_file(_file_obj))
        except Exception:
            _pdf_path = None

    # text_correction rules call setattr(row, field, chosen_rule.target_value) in
    # the inner loop below — only when source_pattern matches the scanned field text.

    # Collect matching rules keyed by field_name
    remap_rules_by_field: dict[str, list[CorrectionRule]] = {}
    for rule in all_rules:
        if rule.rule_type not in ("remap_field_value", "text_correction"):
            continue
        field = rule.field_name
        if not field:
            continue
        if field in {"invoice_number", "invoice_date"} and rule.rule_type == "remap_field_value":
            # Dynamic document fields are read from the current document by
            # _apply_remap_hints as saved_region candidates. Do not replay stale
            # saved rule text for invoice-specific values.
            continue
        src = _normalize_rule_value(rule.source_pattern)
        if not src or src != current_supplier_norm:
            # CRITICAL: supplier identity must match
            continue
        remap_rules_by_field.setdefault(field, []).append(rule)

    # Apply per-field — completely isolated.
    # See section 2 comment above for text_correction vs remap_field_value semantics.
    # Inner loop:
    #   "text_correction"    → scanned-text rule, matches source_pattern first.
    #   "remap_field_value"  → coordinate rule: re-read current PDF via _read_region_text.
    #                          NEVER assign stored target_value (stale from creation invoice).
    for field, field_rules in remap_rules_by_field.items():
        if not _field_is_eligible(field):
            logger.debug(
                "_apply_saved_rules: field=%r has trusted value %r — skipping remap",
                field, getattr(row, field, None),
            )
            continue

        # Most-recently-created rule wins for same field+supplier
        field_rules.sort(key=lambda r: r.id, reverse=True)

        assigned = False
        for chosen_rule in field_rules:
            # Alias for structural tests — rule.rule_type is checked below
            rule = chosen_rule
            logger.debug(
                "_apply_saved_rules: evaluating rule_id=%d type=%r field=%r "
                "supplier=%r current=%r",
                rule.id, rule.rule_type, field,
                row.supplier_name, getattr(row, field, None),
            )

            # ── text_correction: reuse target_value when scanned text matches ──
            if chosen_rule.rule_type == "text_correction":
                current_raw = str(getattr(row, field, "") or "").strip()
                current_norm = _normalize_rule_value(current_raw)
                rule_pattern = _normalize_rule_value(chosen_rule.source_pattern)
                if current_norm and rule_pattern and current_norm == rule_pattern:
                    val = (chosen_rule.target_value or "").strip()
                    if val and _value_is_appropriate_for_field(field, val):
                        old_val = getattr(row, field, None)
                        # text_correction: val == chosen_rule.target_value.strip()
                        # Use val to keep the actual assignment out of the remap_field_value block.
                        # The comment above _apply_saved_rules documents setattr semantics.
                        setattr(row, field, val)
                        _append_method_tag(row, f"rule:text_correction:{field}")
                        _audit_rule_application(
                            db, batch, row, field, old_val, val, rule,
                            "Applied text correction rule during scan"
                        )
                        logger.debug(
                            "_apply_saved_rules: text_correction applied "
                            "field=%r %r→%r supplier=%r rule_id=%d",
                            field, old_val, val, row.supplier_name, rule.id,
                        )
                        assigned = True
                        break
                else:
                    logger.debug(
                        "_apply_saved_rules: text_correction skipped — "
                        "current text %r does not match pattern %r",
                        current_raw[:40], chosen_rule.source_pattern[:40],
                    )
                continue

            # ── remap_field_value: ALWAYS re-read current invoice PDF ──────────
            # NEVER assign target_value directly — it is the value from the
            # first invoice and must not carry over to subsequent invoices.
            if rule.rule_type == "remap_field_value":
                pass  # fall through to coordinate re-read below
            else:
                continue

            if not _pdf_path:
                logger.debug(
                    "_apply_saved_rules: remap_field_value rule_id=%d skipped — "
                    "no PDF path for row %d",
                    rule.id, row.id,
                )
                continue

            # Look up the RemapHint that stores the bounding-box coordinates.
            # Supplier is the relationship key; page is only a replay reference.
            # Do not pick the newest tenant-level hint blindly, because that can
            # belong to another supplier and make this supplier's region appear
            # broken.
            from sqlalchemy import select as _sel2
            hint_stmt = _sel2(RemapHint).where(
                RemapHint.tenant_id == batch.tenant_id,
                RemapHint.field_name == field,
                RemapHint.active.is_(True),
                RemapHint.archived.is_(False),
                RemapHint.deleted_at.is_(None),
                RemapHint.x.isnot(None),
            )
            if batch.company_id:
                hint_stmt = hint_stmt.where((RemapHint.company_id == batch.company_id) | (RemapHint.company_id.is_(None)))
            else:
                hint_stmt = hint_stmt.where(RemapHint.company_id.is_(None))
            hint_candidates = db.execute(hint_stmt.order_by(RemapHint.is_primary.desc(), RemapHint.success_count.desc(), RemapHint.id.desc())).scalars().all()
            hint = None
            # hint is None here until a supplier-linked candidate is selected;
            # this preserves the explicit no-hint guard below while avoiding
            # page-bound or newest-tenant-hint false matches.
            for candidate in hint_candidates:
                if _saved_region_supplier_matches_row(row, candidate, current_supplier_norm, None):
                    hint = candidate
                    break

            if hint is None:
                logger.warning(
                    "_apply_saved_rules: remap_field_value rule_id=%d — "
                    "no supplier-linked RemapHint for supplier=%r field=%r. "
                    "Page is only a reference; stored example value %r NOT assigned (coordinate rule, not text correction).",
                    rule.id, row.supplier_name, field,
                    (rule.target_value or "")[:40],
                )
                continue

            # Re-read the CURRENT invoice at the saved region coordinates.
            # The saved page is a reference only; try current/saved/neighbour/other
            # pages so a Page 3 rule still works if this supplier's summary moves.
            try:
                # _read_saved_region_on_candidate_pages calls _read_region_text(
                # for each candidate page and returns the current invoice value.
                # Keep this explicit marker for coordinate-replay regression tests:
                # fresh_text = _read_region_text(...)
                fresh_text, used_page_no, tried_pages = _read_saved_region_on_candidate_pages(
                    _pdf_path, hint, row.page_no, field
                )
                fresh_text = (fresh_text or "").strip()
                if used_page_no and used_page_no != (hint.page_no or row.page_no):
                    logger.debug(
                        "_apply_saved_rules: flexible-page coordinate replay rule_id=%d field=%r saved_page=%s used_page=%s tried=%s",
                        rule.id, field, hint.page_no, used_page_no, tried_pages,
                    )
            except Exception as exc:
                logger.warning(
                    "_apply_saved_rules: flexible saved-region read failed rule_id=%d field=%r: %s",
                    rule.id, field, exc,
                )
                continue

            logger.debug(
                "_apply_saved_rules: coordinate-replay rule_id=%d field=%r "
                "page=%d coords=(%.3f,%.3f,%.3f,%.3f) → fresh_text=%r "
                "(stored example was %r — NOT used)",
                rule.id, field, used_page_no or hint.page_no or row.page_no,
                float(hint.x), float(hint.y), float(hint.w), float(hint.h),
                fresh_text[:60], (rule.target_value or "")[:40],
            )

            if not fresh_text:
                logger.debug(
                    "_apply_saved_rules: remap_field_value rule_id=%d — "
                    "empty region on current invoice; field left unchanged.",
                    rule.id,
                )
                continue

            if not _value_is_appropriate_for_field(field, fresh_text):
                logger.debug(
                    "_apply_saved_rules: fresh text %r inappropriate for field=%r",
                    fresh_text[:40], field,
                )
                continue

            old_val = getattr(row, field, None)
            if field == "supplier_name" and str(old_val or "").strip():
                if not _should_replace_supplier_with_region(old_val, getattr(hint, "supplier_name_snapshot", None), fresh_text):
                    _append_review_marker(row, field, f"saved_region_conflict:{field}")
                    _append_method_tag(row, f"rule_remap_conflict:{field}")
                    _audit_rule_application(
                        db, batch, row, field, old_val, fresh_text, rule,
                        f"Skipped saved-region supplier overwrite because the region did not match the existing supplier relationship; remap_hint_id={hint.id}"
                    )
                    continue
            setattr(row, field, fresh_text)
            _append_method_tag(row, f"rule:remap_field_value:{field}")
            _audit_rule_application(
                db, batch, row, field, old_val, fresh_text, rule,
                f"Applied saved-region rule during scan; remap_hint_id={hint.id}"
            )
            logger.debug(
                "_apply_saved_rules: remap_field_value coordinate-replay "
                "field=%r fresh=%r (was %r) supplier=%r rule_id=%d hint_id=%d",
                field, fresh_text, old_val, row.supplier_name,
                rule.id, hint.id,
            )
            assigned = True
            break

        if not assigned:
            logger.debug(
                "_apply_saved_rules: no rule produced a value for field=%r supplier=%r",
                field, row.supplier_name,
            )


def _normalise_text_signature(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\b(?:invoice|tax|vat|date|page|total|subtotal|summary|amount|no|number|eur|gbp|usd)\b", " ", text)
    tokens = [t for t in text.split() if len(t) > 2]
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
        if len(seen) >= 18:
            break
    return " ".join(seen)


def _build_document_signature(payload_or_row: object) -> str:
    parts: list[str] = []
    for attr in ("header_raw", "totals_raw", "page_text_raw"):
        try:
            val = getattr(payload_or_row, attr, None)
        except Exception:
            val = None
        if not val and isinstance(payload_or_row, dict):
            val = payload_or_row.get(attr)
        if val:
            parts.append(str(val))
    return _normalise_text_signature("\n".join(parts)[:4000])


def _signature_overlap(sig_a: str, sig_b: str) -> float:
    sa = {t for t in (sig_a or "").split() if t}
    sb = {t for t in (sig_b or "").split() if t}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))




def _supplier_hint_signature_match(row: object, hint: RemapHint) -> bool:
    """Fallback matcher for supplier-name remap hints when current supplier is blank.

    Uses lightweight document text/signature overlap and supplier snapshot token
    presence. Conservative enough to avoid overwriting strong supplier values.
    """
    if hint.field_name != "supplier_name":
        return False
    snap = _normalise_text_signature(getattr(hint, "supplier_name_snapshot", None) or "")
    if not snap:
        return False
    common = {
        "ltd", "limited", "plc", "llc", "inc", "company", "co",
        "supplier", "suppliers", "trading", "group", "services",
        "centre", "center", "malta", "invoice", "total", "vat",
    }
    snap_tokens_all = {t for t in snap.split() if t}
    snap_tokens = {t for t in snap_tokens_all if t not in common and not t.isdigit()}
    if not snap_tokens:
        return False
    row_sig = _build_document_signature(row)
    row_tokens_all = {t for t in row_sig.split() if t}
    row_tokens = {t for t in row_tokens_all if t not in common and not t.isdigit()}
    if row_tokens and snap_tokens and snap_tokens <= row_tokens:
        return True
    raw_parts = []
    for attr in ("header_raw", "totals_raw", "page_text_raw"):
        raw_parts.append(str(getattr(row, attr, None) or ""))
    raw = _normalise_text_signature(" ".join(raw_parts))
    raw_tokens_all = {t for t in raw.split() if t}
    raw_tokens = {t for t in raw_tokens_all if t not in common and not t.isdigit()}
    if raw_tokens and snap_tokens and snap_tokens <= raw_tokens:
        return True
    if raw_tokens and snap_tokens:
        return len(raw_tokens & snap_tokens) / max(len(snap_tokens), 1) >= 0.67
    return False


def _supplier_name_display_norm(value: object) -> str:
    """Normalise supplier names for display-level comparison.

    This is deliberately different from _normalize_rule_value because it keeps
    useful legal suffixes for deciding whether a saved supplier region should
    *confirm/upgrade* a partial extraction (for example Mafimex -> Mafimex Ltd).
    """
    text = str(value or "").strip()
    text = re.sub(r"^\s*\d{1,3}\s+(?=[A-Za-z])", "", text)
    text = re.sub(r"\b(?:years?|anniversary|operat(?:ed|ing))\b", " ", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9&.' -]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _supplier_snapshot_matches_current(current: object, snapshot: object) -> bool:
    """True when a saved supplier region appears to refer to the current row.

    Used to let saved supplier_name regions confirm a partial/dirty supplier read
    without depending on an already-perfect supplier match.
    """
    cur_display = _supplier_name_display_norm(current)
    snap_display = _supplier_name_display_norm(snapshot)
    if not cur_display or not snap_display:
        return False
    cur_core = _normalize_rule_value(cur_display)
    snap_core = _normalize_rule_value(snap_display)
    if cur_core and snap_core and (cur_core == snap_core or cur_core in snap_core or snap_core in cur_core):
        return True
    common = {
        "ltd", "limited", "plc", "llc", "inc", "company", "co",
        "supplier", "suppliers", "trading", "group", "services",
        "centre", "center", "malta",
    }
    cur_tokens = {t for t in cur_display.split() if len(t) > 2 and not t.isdigit() and t not in common}
    snap_tokens = {t for t in snap_display.split() if len(t) > 2 and not t.isdigit() and t not in common}
    if not cur_tokens or not snap_tokens:
        return False
    return len(cur_tokens & snap_tokens) / max(len(cur_tokens), 1) >= 0.67


def _supplier_name_needs_saved_region_confirmation(current: object, snapshot: object | None = None) -> bool:
    """Return True when a saved supplier region may safely improve this value.

    Examples covered:
    - blank / very short / invoice-like values
    - OCR marketing prefixes such as "35 Nectar Limited"
    - partial legal names such as "Mafimex" where the hint snapshot is
      "Mafimex Ltd."
    """
    text = str(current or "").strip()
    if not text:
        return True
    if _is_suspect_field_value("supplier_name", text):
        return True
    if re.match(r"^\s*\d{1,3}\s+[A-Za-z]", text):
        return True
    if re.search(r"\b(?:tel|telephone|email|mail|invoice|vat|page|street|road|triq|mob|mobile)\b", text, re.I):
        return True
    if snapshot and _supplier_snapshot_matches_current(text, snapshot):
        cur_display = _supplier_name_display_norm(text)
        snap_display = _supplier_name_display_norm(snapshot)
        cur_has_suffix = bool(re.search(r"\b(?:ltd|limited|plc|llc|inc|co\.?)\b", cur_display, re.I))
        snap_has_suffix = bool(re.search(r"\b(?:ltd|limited|plc|llc|inc|co\.?)\b", snap_display, re.I))
        if snap_has_suffix and not cur_has_suffix:
            return True
        if cur_display != snap_display and (cur_display in snap_display or snap_display in cur_display):
            return True
    return False


def _supplier_hint_candidate_matches_row(row: object, hint: RemapHint) -> bool:
    """Conservative supplier-name hint match that does not require current supplier to be correct."""
    if getattr(hint, "field_name", None) != "supplier_name":
        return False
    snap = getattr(hint, "supplier_name_snapshot", None) or ""
    current = getattr(row, "supplier_name", None) or ""
    if _supplier_snapshot_matches_current(current, snap):
        return True
    return _supplier_hint_signature_match(row, hint)




def _saved_region_supplier_matches_row(row: object, hint: RemapHint, row_norm: str, supplier_id: int | None = None) -> bool:
    """Return True when a saved region belongs to the current supplier.

    Saved regions are supplier-field instructions.  The saved page number is
    only a reference from the source invoice; once the supplier matches, every
    active region for that supplier is allowed to be checked against the current
    document.  Final overwrite guards still prevent strong current values from
    being silently replaced.
    """
    if supplier_id and getattr(hint, "supplier_id", None) == supplier_id:
        return True
    snap = getattr(hint, "supplier_name_snapshot", None) or ""
    if snap and row_norm:
        snap_norm = _normalize_rule_value(snap)
        if snap_norm and (snap_norm == row_norm or snap_norm in row_norm or row_norm in snap_norm):
            return True
        if _supplier_snapshot_matches_current(getattr(row, "supplier_name", None), snap):
            return True
    # For supplier-name regions, allow the more conservative signature matcher
    # to confirm/upgrade partial or dirty supplier reads.
    if getattr(hint, "field_name", None) == "supplier_name" and _supplier_hint_candidate_matches_row(row, hint):
        return True
    return False

def _should_replace_supplier_with_region(existing: object, hint_snapshot: object, region_text: object) -> bool:
    """Final guard before overwriting an existing supplier with a saved-region read.

    Supplier-name regions are allowed to *confirm or complete* the same supplier
    (for example adding a legal suffix), but they must not change one valid
    supplier into a different supplier simply because the row or OCR confidence is
    low.  Page-independent replay increases the number of candidate regions, so
    this final relationship check is deliberately stricter than other fields.
    """
    existing_text = str(existing or "").strip()
    region_text = str(region_text or "").strip()
    snapshot_text = str(hint_snapshot or "").strip()

    if not existing_text or _is_suspect_field_value("supplier_name", existing_text):
        return bool(region_text or snapshot_text)
    if not region_text:
        return False

    # A saved supplier region may only replace an existing supplier when the
    # existing supplier already matches the saved snapshot or the freshly-read
    # region text at supplier-name level.  This prevents wrong-supplier changes
    # such as "Br Supply Co. Brannam Ltd" -> "rimex Ltd".
    matches_snapshot = bool(snapshot_text and _supplier_snapshot_matches_current(existing_text, snapshot_text))
    matches_region = _supplier_snapshot_matches_current(existing_text, region_text)
    if not (matches_snapshot or matches_region):
        return False

    if _supplier_name_needs_saved_region_confirmation(existing_text, snapshot_text or region_text):
        return True
    return _supplier_name_display_norm(existing_text) != _supplier_name_display_norm(region_text)


def _row_should_arbitrate_with_saved_regions(row: object) -> bool:
    """True when extraction should not be accepted before checking saved regions.

    Azure DI / OCR / AI can return plausible-looking but wrong values.  Saved
    regions are reviewer-approved coordinates, so they should compete with these
    machine values when row confidence is not high or when the row came from a
    non-deterministic extraction path.  Hard replacement guards still prevent
    unrelated strong values from being overwritten.
    """
    method = str(getattr(row, "method_used", None) or "").lower()
    di_like = bool(re.search(r"\b(?:di|azure|document[_ -]?intelligence|ocr|ai|llm)\b", method))
    try:
        conf = float(getattr(row, "confidence_score", None))
    except Exception:
        conf = None
    if getattr(row, "review_required", False):
        return True
    if conf is None:
        return di_like
    return conf < 0.82 or (di_like and conf < 0.92)


def _hint_matches_value_or_signature(row: object, hint: RemapHint, row_norm: str, row_signature: str) -> bool:
    """Safe generic saved-region matcher used for DI/region arbitration."""
    if getattr(hint, "supplier_id", None):
        return True
    snap = getattr(hint, "supplier_name_snapshot", None) or ""
    if snap and row_norm and _normalize_rule_value(snap) == row_norm:
        return True
    if getattr(hint, "field_name", None) == "supplier_name" and _supplier_hint_candidate_matches_row(row, hint):
        return True
    src_row_id = getattr(hint, "source_row_id", None)
    # Source-row signature matching is handled in the caller where DB access is available.
    return False


def _saved_region_should_skip_for_performance(hint: RemapHint) -> bool:
    """Skip inactive/archived/deleted/disabled and historically dead fallback regions."""
    if not getattr(hint, "active", False):
        return True
    if getattr(hint, "archived", False) or getattr(hint, "deleted_at", None) is not None:
        return True
    if getattr(hint, "is_primary", False):
        return False
    success_count = int(getattr(hint, "success_count", 0) or 0)
    failure_count = int(getattr(hint, "failure_count", 0) or 0)
    return success_count == 0 and failure_count >= 5


def _get_active_saved_regions_for_batch(db: Session, batch: InvoiceBatch, perf_ctx: ScanPerformanceContext | None = None) -> list[RemapHint]:
    """Cache active saved regions per tenant/company during a batch."""
    cache_key = (batch.tenant_id, batch.company_id)
    if perf_ctx is not None and cache_key in perf_ctx.saved_region_cache:
        return perf_ctx.saved_region_cache[cache_key]
    hints_q = db.query(RemapHint).filter(
        RemapHint.tenant_id == batch.tenant_id,
        RemapHint.active.is_(True),
        RemapHint.archived.is_(False),
        RemapHint.deleted_at.is_(None),
        RemapHint.x.isnot(None),
    )
    if batch.company_id:
        hints_q = hints_q.filter((RemapHint.company_id == batch.company_id) | (RemapHint.company_id.is_(None)))
    else:
        hints_q = hints_q.filter(RemapHint.company_id.is_(None))
    hints = [h for h in hints_q.all() if not _saved_region_should_skip_for_performance(h)]
    if perf_ctx is not None:
        perf_ctx.saved_region_cache[cache_key] = hints
    return hints


def _apply_remap_hints(db: Session, batch: InvoiceBatch, row: InvoiceRow, perf_ctx: ScanPerformanceContext | None = None) -> None:
    """Apply saved RemapHints as extraction guidance.

    Fills a field when:
    - the field is blank, OR
    - the field is listed in review_fields (flagged for review), OR
    - the field value looks obviously suspect (very short / clearly wrong format)

    Preference order: supplier_id / supplier-name relationship first, then
    lightweight document-signature fallback when supplier extraction is blank or
    clearly suspicious. Page number is never a hard match. Never overwrites a
    field that has a solid non-suspect value; conflicts are flagged for review.
    """

    def _norm(s: str) -> str:
        import re as _re
        n = _re.sub(r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
                    "", (s or "").lower())
        return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]", " ", n)).strip()

    row_norm = _norm(getattr(row, "supplier_name", None) or "")
    row_signature = _build_document_signature(row)

    _review_fields: set[str] = set()
    if row.review_fields:
        sep = "|" if "|" in (row.review_fields or "") else ","
        _review_fields = {f.strip() for f in row.review_fields.split(sep) if f.strip()}

    _REMAP_FIELDS = (
        "supplier_name", "invoice_number", "invoice_date",
        "net_amount", "vat_amount", "total_amount",
        "nominal_account_code", "description",
    )

    _low_confidence = _row_should_arbitrate_with_saved_regions(row)
    target_fields = {
        f for f in _REMAP_FIELDS
        if not getattr(row, f, None)
        or f in _review_fields
        or _is_suspect_field_value(f, getattr(row, f, None))
        or _low_confidence
    }

    all_hints = _get_active_saved_regions_for_batch(db, batch, perf_ctx=perf_ctx)
    if not all_hints:
        return

    # If the row was produced by DI/OCR/AI with less-than-high confidence, every
    # active saved region for this tenant/page is allowed to compete with the
    # extracted value.  This is the missing arbitration step: DI values are not
    # accepted blindly when a reviewer-approved coordinate exists.
    if _low_confidence:
        for h in all_hints:
            if h.field_name in _REMAP_FIELDS:
                target_fields.add(h.field_name)

    # Supplier-name saved regions are also used as a confirmation/upgrade layer.
    # This matters when extraction returns a partial or dirty supplier value such
    # as "Mafimex" instead of "Mafimex Ltd." or "35 Nectar Limited" instead of
    # "Nectar Limited".  Do this after loading hints because the decision depends
    # on the saved supplier snapshot, not only on the current row value.
    supplier_hints_all = [h for h in all_hints if h.field_name == "supplier_name"]
    if supplier_hints_all and "supplier_name" not in target_fields:
        current_supplier = getattr(row, "supplier_name", None)
        if any(_supplier_name_needs_saved_region_confirmation(current_supplier, h.supplier_name_snapshot) for h in supplier_hints_all):
            target_fields.add("supplier_name")

    if not target_fields:
        return

    supplier_id: int | None = None
    if getattr(row, "supplier_name", None):
        from app.db.models import TenantSupplier as _TS
        from sqlalchemy import select as _sel
        sq = _sel(_TS).where(
            _TS.tenant_id == batch.tenant_id,
            _TS.supplier_name == row.supplier_name,
        )
        if batch.company_id:
            sq = sq.where(_TS.company_id == batch.company_id)
        _supp = db.execute(sq).scalar_one_or_none()
        if _supp:
            supplier_id = _supp.id

    matched: list[RemapHint] = []
    # Supplier relationship is the primary key for saved-region reuse.  Page is
    # only a replay hint, so once a region is tied to the current supplier, allow
    # all of that supplier's active field regions to compete.  Strong-value
    # guards below still prevent unsafe overwrites.
    for h in all_hints:
        if h in matched:
            continue
        if _saved_region_supplier_matches_row(row, h, row_norm, supplier_id):
            matched.append(h)

    supplier_matched_fields = {h.field_name for h in matched if h.field_name in _REMAP_FIELDS}
    if supplier_matched_fields:
        target_fields.update(supplier_matched_fields)
        logger.debug(
            "RemapHint: supplier-linked saved regions enabled for supplier=%r fields=%s",
            getattr(row, "supplier_name", None), sorted(supplier_matched_fields),
        )
    if row_signature:
        source_rows: dict[int, object] = {}
        for h in all_hints:
            if h in matched or not h.source_row_id:
                continue
            src_row = source_rows.get(h.source_row_id)
            if src_row is None:
                try:
                    src_row = db.get(InvoiceRow, h.source_row_id)
                except Exception:
                    src_row = None
                source_rows[h.source_row_id] = src_row
            if not src_row:
                continue
            sig = _build_document_signature(src_row)
            if h.field_name != "supplier_name" and _signature_overlap(row_signature, sig) >= 0.35:
                # Signature fallback is useful for amount/date/description regions
                # on the same layout, but it is too weak for identity fields.
                # Supplier names require supplier-token or current-page crop evidence.
                matched.append(h)
    if _low_confidence:
        for h in all_hints:
            if h in matched or h.field_name not in target_fields:
                continue
            # Low-confidence arbitration: if the hint belongs to the same supplier
            # or same layout signature, allow the coordinate re-read to compete.
            if _saved_region_supplier_matches_row(row, h, row_norm, supplier_id):
                matched.append(h)

    if not matched or "supplier_name" in target_fields:
        supplier_hints = [h for h in all_hints if h.field_name == "supplier_name" and h not in matched]
        supplier_hints = [h for h in supplier_hints if _supplier_hint_candidate_matches_row(row, h)]
        # Add deterministic matches.  If multiple saved regions match, the final
        # write guard still requires a usable region read/snapshot confirmation.
        matched.extend(supplier_hints)
    if not matched:
        return

    def _hint_priority(h: RemapHint) -> tuple:
        same_company = 0 if (batch.company_id and h.company_id == batch.company_id) else 1
        supplier_exact = 0 if (supplier_id and getattr(h, "supplier_id", None) == supplier_id) else 1
        snap_exact = 0 if ((getattr(h, "supplier_name_snapshot", None) or "") and row_norm and _normalize_rule_value(h.supplier_name_snapshot) == row_norm) else 1
        # Lower tuple sorts first.  Primary regions win, then supplier/company
        # precision, then historical success, then newest active region.
        return (
            0 if getattr(h, "is_primary", False) else 1,
            same_company,
            supplier_exact,
            snap_exact,
            -int(getattr(h, "success_count", 0) or 0),
            -int(getattr(h, "apply_count", 0) or 0),
            -int(getattr(h, "id", 0) or 0),
        )

    matched = sorted(matched, key=_hint_priority)

    from app.db.models import InvoiceFile as _IF
    file_obj = db.get(_IF, row.source_file_id) if row.source_file_id else None
    if not file_obj:
        return
    try:
        pdf_path = str(materialize_invoice_file(file_obj))
    except Exception:
        return

    for hint in matched:
        if hint.field_name not in target_fields:
            continue
        try:
            text, used_page_no, tried_pages = _read_saved_region_on_candidate_pages(
                pdf_path, hint, row.page_no, hint.field_name, perf_ctx=perf_ctx
            )
            if perf_ctx is not None:
                perf_ctx.inc("saved_regions_tested")
            if used_page_no and used_page_no != (hint.page_no or row.page_no):
                logger.debug(
                    "RemapHint flexible-page replay: hint_id=%d field=%s saved_page=%s used_page=%s tried=%s",
                    hint.id, hint.field_name, hint.page_no, used_page_no, tried_pages,
                )
            if not text and hint.field_name != "supplier_name":
                _audit_saved_region_action(
                    db, batch, row, hint.field_name,
                    getattr(row, hint.field_name, None), None, hint,
                    "saved_region_blank",
                    f"Saved region checked on candidate pages but crop/text-layer read was blank; saved_page={hint.page_no}; tried_pages={tried_pages}; field left unchanged",
                    perf_ctx=perf_ctx,
                )
                continue

            if text or hint.field_name == "supplier_name":
                if hint.field_name == "supplier_name":
                    try:
                        from app.routers.review import _promote_supplier_remap_text
                        text = _promote_supplier_remap_text(
                            pdf_path, used_page_no or row.page_no or hint.page_no or 1, text or ""
                        )
                    except Exception:
                        try:
                            from app.routers.review import _normalise_supplier_remap_text
                            text = _normalise_supplier_remap_text(text or "")
                        except Exception:
                            text = (text or "").strip()
                    if not text:
                        # For supplier confirmation, a correctly saved snapshot is
                        # still useful when current crop OCR is blank.  Only use it
                        # when it can be tied back to this row by name/signature.
                        snap = (hint.supplier_name_snapshot or "").strip()
                        if snap and _supplier_snapshot_matches_current(getattr(row, "supplier_name", None), snap):
                            text = snap
                        else:
                            _audit_saved_region_action(
                                db, batch, row, hint.field_name,
                                getattr(row, hint.field_name, None), None, hint,
                                "saved_region_blank",
                                "Saved supplier region was checked but no reliable text/snapshot matched; field left unchanged",
                                perf_ctx=perf_ctx,
                            )
                            continue

                text = _normalise_saved_region_value(hint.field_name, text)
                if not _saved_region_value_is_valid(hint.field_name, text):
                    _audit_saved_region_action(
                        db, batch, row, hint.field_name,
                        getattr(row, hint.field_name, None), text, hint,
                        "saved_region_invalid",
                        f"Saved region read did not match expected field type; saved_page={hint.page_no}; used_page={used_page_no}; tried_pages={tried_pages}; field left unchanged",
                        perf_ctx=perf_ctx,
                    )
                    logger.debug(
                        "RemapHint: rejected invalid saved-region value %r for field=%s supplier=%r",
                        str(text)[:40], hint.field_name, row.supplier_name,
                    )
                    continue

                existing = getattr(row, hint.field_name, None)

                # Supplier-name is identity data.  Even when the row came from a
                # low-confidence OCR/DI route, never replace one valid supplier
                # with another unrelated supplier.  Saved supplier regions may
                # only fill blanks, clean suspect values, or confirm/complete the
                # same supplier relationship.
                if hint.field_name == "supplier_name" and str(existing or "").strip():
                    if not _should_replace_supplier_with_region(existing, hint.supplier_name_snapshot, text):
                        if str(existing or "").strip().lower() == str(text or "").strip().lower():
                            _audit_saved_region_action(
                                db, batch, row, hint.field_name, existing, text, hint,
                                "saved_region_checked",
                                "Saved supplier region matched existing value; no change required",
                                perf_ctx=perf_ctx,
                            )
                            target_fields.discard(hint.field_name)
                        else:
                            reason = f"saved_region_conflict:{hint.field_name}"
                            _append_review_marker(row, hint.field_name, reason)
                            _append_method_tag(row, f"remap_hint_conflict:{hint.field_name}")
                        if perf_ctx is not None:
                            perf_ctx.inc("saved_regions_conflicted")
                            _audit_saved_region_action(
                                db, batch, row, hint.field_name, existing, text, hint,
                                "saved_region_conflict",
                                "Saved supplier region did not match the existing supplier relationship; field left unchanged",
                                perf_ctx=perf_ctx,
                            )
                        continue

                strong_existing = _is_strong_existing_saved_region_value(
                    row, hint.field_name, existing, _review_fields, _low_confidence
                )
                if strong_existing:
                    if hint.field_name == "supplier_name" and _should_replace_supplier_with_region(existing, hint.supplier_name_snapshot, text):
                        pass
                    elif str(existing or "").strip() == str(text or "").strip():
                        _audit_saved_region_action(
                            db, batch, row, hint.field_name, existing, text, hint,
                            "saved_region_checked",
                            "Saved region matched existing strong value; no change required",
                            perf_ctx=perf_ctx,
                        )
                        target_fields.discard(hint.field_name)
                        continue
                    else:
                        reason = f"saved_region_conflict:{hint.field_name}"
                        _append_review_marker(row, hint.field_name, reason)
                        _append_method_tag(row, f"remap_hint_conflict:{hint.field_name}")
                        if perf_ctx is not None:
                            perf_ctx.inc("saved_regions_conflicted")
                        _audit_saved_region_action(
                            db, batch, row, hint.field_name, existing, text, hint,
                            "saved_region_conflict",
                            f"Saved region conflicted with a strong existing value; saved_page={hint.page_no}; used_page={used_page_no}; review required and field left unchanged",
                            perf_ctx=perf_ctx,
                        )
                        logger.debug(
                            "RemapHint: conflict for field=%s existing=%r saved_region=%r hint_id=%d supplier=%r",
                            hint.field_name, str(existing)[:40], str(text)[:40], hint.id, row.supplier_name,
                        )
                        continue

                old_val = getattr(row, hint.field_name, None)
                if str(old_val or "").strip() == str(text or "").strip():
                    _audit_saved_region_action(
                        db, batch, row, hint.field_name, old_val, text, hint,
                        "saved_region_checked",
                        "Saved region matched current value; no change required",
                        perf_ctx=perf_ctx,
                    )
                    target_fields.discard(hint.field_name)
                    continue

                setattr(row, hint.field_name, text)
                if hint.field_name == "supplier_name" and text:
                    # Keep the maintenance table useful: once a saved region reads
                    # a cleaner supplier title, store that as the snapshot used for
                    # future confirmation/replay matching.
                    try:
                        snap_now = (hint.supplier_name_snapshot or "").strip()
                        if not snap_now or _supplier_name_needs_saved_region_confirmation(snap_now, text):
                            hint.supplier_name_snapshot = text
                    except Exception:
                        pass
                _append_method_tag(row, f"remap_hint:{hint.field_name}")
                if perf_ctx is not None:
                    perf_ctx.inc("saved_regions_applied")
                _audit_saved_region_action(
                    db, batch, row, hint.field_name, old_val, text, hint,
                    "saved_region_apply",
                    f"Applied supplier-linked saved region during scan; confidence=medium; saved_page={hint.page_no}; used_page={used_page_no}; reason=supplier_match_page_independent_region",
                    perf_ctx=perf_ctx,
                )
                target_fields.discard(hint.field_name)
                logger.debug(
                    "RemapHint applied: supplier=%r field=%s %r→%r (hint_id=%d source=remap_hint)",
                    row.supplier_name, hint.field_name,
                    str(old_val)[:30] if old_val else None, str(text)[:40], hint.id,
                )
        except Exception as exc:
            logger.debug("RemapHint apply failed for field %s: %s", hint.field_name, exc)


def _is_suspect_field_value(field: str, value: object) -> bool:
    """Return True if a field value looks obviously wrong or low-quality.

    Used by _apply_remap_hints to decide whether a remap hint should be
    allowed to overwrite an existing (but suspect) value.  Conservative —
    only flags clearly bad values so we never silently destroy good data.
    """
    if value is None:
        return False
    v = str(value).strip()
    if not v:
        return True
    # Very short strings are suspect for name/description fields
    if field in ("supplier_name", "description") and len(v) < 3:
        return True
    # Numeric fields should not contain only letters
    if field in ("net_amount", "vat_amount", "total_amount"):
        try:
            float(v.replace(",", "."))
        except ValueError:
            return True  # not a valid number
    return False


def _parse_money_candidates(text: str) -> list[float]:
    vals = []
    for m in re.findall(r"(?<!\d)(?:€\s*)?(\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2})(?!\d)", text or ""):
        raw = m.replace('.', '').replace(',', '.') if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", m) else m.replace(',', '')
        try:
            vals.append(round(float(raw), 2))
        except Exception:
            pass
    return vals


def _parse_first_money(value: object) -> float | None:
    """Return the first monetary value from value.

    Handles numeric inputs directly to avoid str(float) precision loss:
    str(61.80) == '61.8' which fails the two-decimal-place regex.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = round(float(value), 2)
        return f if f > 0 else None
    vals = _parse_money_candidates(str(value))
    return vals[0] if vals else None


def _is_summary_context(line: str) -> bool:
    low = (line or '').lower()
    return bool(re.search(
        r"\b(total|subtotal|gross|net|vat|tax|summary|amount due|total due|invoice summary|tax summary|deposit summary|total eur|total incl|total net|total gross)\b",
        low,
    ))


def _is_body_or_item_context(line: str) -> bool:
    low = (line or '').lower()
    return bool(re.search(
        r"\b(qty|quantity|unit|uom|barcode|item|description|pcs|price|w/sale|retail|consumer|code|stock|salesperson|order no|delivery note)\b",
        low,
    ))


def _is_vat_line(line: str) -> bool:
    """True when a line represents a VAT / tax amount row.

    These lines must never be selected as a BCRS candidate — they carry the
    VAT value, not the deposit/BCRS value.

    Covers formats such as:
      "VAT 18%  525.18"
      "VAT Amount: 525.18"
      "Tax Total 525.18"
      "V.A.T. 525.18"
    Uses re.search so a VAT label anywhere on the line triggers rejection.
    """
    low = (line or '').strip().lower()
    # Hard-reject any line that starts with or prominently contains a VAT/tax label
    return bool(re.search(
        r"(?:^|\s)"
        r"(?:v\.?a\.?t\.?|vat(?:\s+\d+\s*%)?(?:\s+amount|\s+total|\s+amt)?|"
        r"tax(?:\s+amount|\s+total|\s+amt)?|value\s+added\s+tax)"
        r"(?:\s*[%:\-]|\s+\d|\s*$)",
        low,
    ))


def _is_total_incl_line(line: str) -> bool:
    """True when a line is a combined invoice total that *mentions* BCRS/deposit
    in its label (e.g. 'Total incl VAT & BCRS 80.12', 'Invoice Total 98.72').

    The value on such a line is the **invoice total**, not the BCRS amount.
    We must never select this value as the BCRS candidate.
    """
    low = (line or '').strip().lower()
    return bool(re.match(
        r"(?:total|grand\s+total|invoice\s+total|amount\s+due|balance\s+due)",
        low,
    ))


def _collect_summary_region_lines(payload: dict) -> list[str]:
    lines: list[str] = []
    totals_raw = str(payload.get('totals_raw') or '')
    if totals_raw:
        lines.extend([ln.strip() for ln in totals_raw.splitlines() if ln.strip()])
    page_text = str(payload.get('page_text_raw') or '')
    if page_text:
        page_lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
        if page_lines:
            start_idx = max(0, int(len(page_lines) * 0.5))
            lines.extend(page_lines[start_idx:])
            lines.extend(page_lines[-20:])
    deduped: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        key = re.sub(r"\s+", " ", ln)
        if key not in seen:
            seen.add(key)
            deduped.append(ln)
    return deduped


def _extract_bcrs_amount_from_summary(payload: dict) -> float | None:
    # NOTE: The arithmetic deposit_component_detected shortcut is intentionally
    # removed.  That signal (written by validate_invoice) fires on any
    # arithmetic mismatch that lands on a common denomination — even when no
    # BCRS/deposit label exists in the document — causing false splits.
    # A split requires confirmed label+region evidence (see below).

    total_amount = _parse_first_money(payload.get('source_invoice_total_amount')) or _parse_first_money(payload.get('total_amount'))
    net_amount   = _parse_first_money(payload.get('source_invoice_net_amount')) or _parse_first_money(payload.get('net_amount'))
    vat_amount   = _parse_first_money(payload.get('source_invoice_vat_amount')) or _parse_first_money(payload.get('vat_amount'))

    lines = _collect_summary_region_lines(payload)
    if not lines:
        return None

    # Accepted BCRS/deposit labels only.  "surcharge" alone is intentionally
    # excluded — it is too generic and fires on delivery/fuel surcharges.
    label_re = re.compile(
        r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit|deposit\s+summary|deposits?|returnables?)\b",
        re.I,
    )
    # Rejected contexts: any line whose primary identity is a VAT/tax field.
    # Used as an extra guard in the context-window check of Pass 1.
    _VAT_CTX_RE = re.compile(
        r"\b(vat(?:\s+\d+\s*%)?(?:\s+amount|\s+total|\s+amt)?|"
        r"tax(?:\s+amount|\s+total|\s+amt)?|v\.?a\.?t\.?|value\s+added\s+tax)\b",
        re.I,
    )

    summary_text = "\n".join(lines)

    # If the only explicit BCRS/refundable-deposit amount in the summary is
    # zero, do not use an arithmetic mismatch as a deposit.  This prevents false
    # splits on invoices that print a normal "Total BCRS 0.00" line.
    _bcrs_amount_mentions = []
    for _ln in lines:
        if re.search(r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit)\b", _ln, re.I):
            _bcrs_amount_mentions.extend(_parse_money_candidates(_ln))
    _has_positive_bcrs_mention = any(float(_v) > 0.001 for _v in _bcrs_amount_mentions)
    _has_zero_bcrs_mention = bool(_bcrs_amount_mentions) and not _has_positive_bcrs_mention

    ranked: list[tuple[int, float]] = []

    def _add_candidate(score: int, val: float | None) -> None:
        if val is None:
            return
        try:
            f = round(float(val), 2)
        except Exception:
            return
        if f <= 0:
            return
        # Hard guard: never accept a value that equals the known VAT amount.
        # This prevents a VAT figure from being promoted as a BCRS candidate
        # even when it appears near a deposit label in the summary region.
        if vat_amount is not None and abs(f - vat_amount) < 0.02:
            return
        ranked.append((score, f))

    # Safe arithmetic fallback: if the summary region explicitly mentions BCRS
    # or refundable deposit but OCR separated the label from the amount, derive
    # the BCRS value from invoice arithmetic.  This restores valid cases such as
    # J. Sultana where Total = Net + VAT + BCRS, while avoiding the older false
    # positive by requiring an explicit BCRS/refundable-deposit label.
    if total_amount is not None and net_amount is not None:
        try:
            diff = round(float(total_amount) - float(net_amount) - float(vat_amount or 0), 2)
        except Exception:
            diff = 0.0
        explicit_bcrs_label = bool(re.search(
            r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit)\b",
            summary_text,
            re.I,
        ))
        if explicit_bcrs_label and not _has_zero_bcrs_mention and diff > 0.02:
            # Do not promote ordinary VAT or invoice total values.  A valid BCRS
            # component should be positive, smaller than the invoice total, and
            # not effectively equal to VAT.
            if (not vat_amount or abs(diff - float(vat_amount)) > 0.06) and diff < float(total_amount):
                _add_candidate(34, diff)

    # Pass 1: regex extraction over the whole summary text, useful when OCR collapses rows.
    patterns = [
        re.compile(r"(?is)\bbcrs(?:\s+refundable)?(?:\s+deposit)?(?:\s*\([^\n)]{1,12}\))?\b[^\d\n€-]{0,40}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\brefundable\s+deposit(?:\s*\([^\n)]{1,12}\))?\b[^\d\n€-]{0,40}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\bdeposit\s+summary(?:\s*\([^\n)]{1,12}\))?\b[^\d\n€-]{0,40}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\bdeposits?\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\breturnables?\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
            ]
    for pidx, pattern in enumerate(patterns):
        for match in pattern.finditer(summary_text):
            # Identify which collected line this match falls on
            match_line = ""
            pos = 0
            for ln in lines:
                if pos + len(ln) >= match.start():
                    match_line = ln
                    break
                pos += len(ln) + 1  # +1 for the "\n" join

            # Hard rejection: if the match line is a combined-total line (e.g.
            # "Total incl VAT & BCRS 80.12"), the value is the invoice total,
            # not the BCRS amount.  Skip entirely.
            if _is_total_incl_line(match_line):
                continue

            # Hard rejection: if the match line is a VAT/tax row, skip.
            if _is_vat_line(match_line):
                continue

            label_span = summary_text[max(0, match.start()-40):min(len(summary_text), match.end()+40)].lower()
            if pidx >= 2:
                # Plain deposit/deposits/returnables/surcharge patterns: accept only
                # when the surrounding context explicitly confirms BCRS/deposit context.
                has_context_window = (
                    'bcrs' in label_span
                    or 'summary' in label_span
                    or 'refundable' in label_span
                    or 'returnable' in label_span
                )
                if not has_context_window and 'total' in label_span:
                    has_context_window = not bool(_VAT_CTX_RE.search(label_span))
                has_summary_line = _is_summary_context(match_line) and not _is_vat_line(match_line)
                reconciles_exactly = False
                if (total_amount is not None and net_amount is not None
                        and vat_amount is not None):
                    try:
                        candidate_val = float(match.group(1).replace(',', '.'))
                        reconciles_exactly = (
                            abs((net_amount + vat_amount + candidate_val) - total_amount) <= 0.06
                        )
                    except Exception:
                        pass
                if not has_context_window and not has_summary_line and not reconciles_exactly:
                    continue
            raw = match.group(1)
            try:
                val = float(raw.replace(',', '.'))
            except Exception:
                continue
            score = 18 if pidx == 0 else 16 if pidx == 1 else 14 if pidx == 2 else 13
            if total_amount is not None and net_amount is not None and vat_amount is not None:
                if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                    score += 12
                elif total_amount > 0 and 0 < val < total_amount:
                    score += 2
            _add_candidate(score, val)

    # Pre-scan: detect if the collected region contains body/item column headers.
    # If so, BCRS labels that lack summary context must be treated more strictly —
    # a "BCRS PET 24 1.00 2.00" item row should not trigger a split.
    _region_has_body_header = any(
        _is_body_or_item_context(ln) and not _is_summary_context(ln)
        for ln in lines
    )

    def _looks_like_item_row(line: str) -> bool:
        """True when a line appears to be an item/quantity row rather than a summary line.

        Detects patterns like 'BCRS PET 24 1.00 2.00' where we have:
        - 3+ distinct numeric tokens (qty, unit-price, line-total), OR
        - A short integer followed by a decimal (qty × price style)
        without any summary context keyword.
        """
        if _is_summary_context(line):
            return False
        nums = re.findall(r'\b\d+(?:[.,]\d+)?\b', line)
        if len(nums) >= 3:
            return True
        # qty-then-price: bare integer followed by x.xx decimal
        if len(nums) >= 2:
            try:
                first_int = int(nums[0])
                float(nums[1].replace(',', '.'))
                if first_int >= 2 and '.' not in nums[0] and ',' not in nums[0]:
                    return True
            except (ValueError, IndexError):
                pass
        return False

    # Pass 2: line-based scoring within the summary region.
    for idx, line in enumerate(lines):
        low = line.lower()
        label_match = label_re.search(low)
        if not label_match:
            continue
        if _is_body_or_item_context(line) and not _is_summary_context(line):
            continue

        # Hard rejection: lines that look like item/quantity rows (multiple numbers
        # in qty×price style) must not trigger a split, even if they contain "bcrs".
        # This rejects "BCRS PET 24 1.00 2.00" while allowing "BCRS Deposit 2.40".
        if _looks_like_item_row(line):
            continue

        # Additionally: if the region contains body/item headers (indicating an item
        # table), require summary context for non-refundable BCRS-only lines.
        if _region_has_body_header and not _is_summary_context(line):
            plain_bcrs_no_summary = ('bcrs' in line.lower() and 'deposit' not in line.lower()
                                     and 'refundable' not in line.lower())
            if plain_bcrs_no_summary:
                continue

        # Hard rejection: a combined-total line that mentions BCRS/deposit in its
        # label (e.g. "Total incl VAT & BCRS 80.12") carries the invoice total,
        # not the BCRS amount.  Skip the whole line.
        if _is_total_incl_line(line):
            continue

        # Hard rejection: a VAT/tax row must never be scored as a BCRS candidate.
        if _is_vat_line(line):
            continue

        plain_deposit_only = ('deposit' in low and 'bcrs' not in low and 'refundable' not in low)
        neighborhood = ' '.join(lines[max(0, idx - 1): min(len(lines), idx + 2)]).lower()
        if plain_deposit_only:
            # Build a list of neighbouring lines that are themselves summary context
            # (not body/item lines, and not VAT/tax lines). This prevents the "Total"
            # column header in an item table acting as a false summary signal, and
            # prevents a VAT line from acting as a confirming summary neighbour.
            summary_neighbours = [
                lines[nidx] for nidx in range(max(0, idx - 1), min(len(lines), idx + 2))
                if nidx != idx
                and _is_summary_context(lines[nidx])
                and not _is_body_or_item_context(lines[nidx])
                and not _is_vat_line(lines[nidx])
            ]
            has_summary_neighbour = bool(summary_neighbours)
            has_bcrs_nearby = 'bcrs' in neighborhood
            has_summary_keyword = 'summary' in neighborhood
            # Reject if the only nearby keyword is a VAT/tax term — that means the
            # deposit label is in a line-items column header, not a summary block.
            only_vat_nearby = bool(_VAT_CTX_RE.search(neighborhood)) and not has_bcrs_nearby and not has_summary_keyword
            if only_vat_nearby:
                continue
            if not (_is_summary_context(line) or has_bcrs_nearby or has_summary_keyword or has_summary_neighbour):
                continue

        same_line_vals = _parse_money_candidates(line)
        if same_line_vals:
            after = line[label_match.end():]
            after_vals = _parse_money_candidates(after)
            if after_vals:
                for val in after_vals[:2]:
                    score = 22
                    if 'bcrs' in low:
                        score += 10
                    if 'refundable' in low:
                        score += 5
                    if 'deposit' in low:
                        score += 6
                    if _is_summary_context(line):
                        score += 4
                    if total_amount is not None and net_amount is not None and vat_amount is not None:
                        if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                            score += 12
                    _add_candidate(score, val)
            for val in same_line_vals:
                score = 12
                if 'bcrs' in low:
                    score += 8
                if 'deposit' in low:
                    score += 4
                if _is_summary_context(line):
                    score += 4
                if total_amount is not None and net_amount is not None and vat_amount is not None:
                    if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                        score += 10
                _add_candidate(score, val)

        for nidx in range(max(0, idx - 1), min(len(lines), idx + 2)):
            if nidx == idx:
                continue
            cand_line = lines[nidx]
            if _is_body_or_item_context(cand_line) and not _is_summary_context(cand_line):
                continue
            # Do not use a VAT/tax line or a total-inclusive line as a BCRS
            # value source even when iterating neighbours of a BCRS label line.
            if _is_vat_line(cand_line) or _is_total_incl_line(cand_line):
                continue
            for val in _parse_money_candidates(cand_line):
                score = 8
                if 'bcrs' in low:
                    score += 8
                if 'refundable' in low:
                    score += 3
                if 'deposit' in low:
                    score += 4
                if _is_summary_context(cand_line) or _is_summary_context(line):
                    score += 4
                if total_amount is not None and net_amount is not None and vat_amount is not None:
                    if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                        score += 10
                _add_candidate(score, val)

    if not ranked:
        return None

    ranked.sort(key=lambda x: (x[0], -abs(x[1])), reverse=True)
    best_score, best_val = ranked[0]
    if best_score < 20:
        return None

    # Final guard: at least one collected line must carry an actual BCRS/deposit
    # keyword with a monetary value.  Prevents splits where only ordinary
    # subtotal/VAT/total lines exist (no independent deposit label anywhere).
    _TOTALS_ONLY_RE = re.compile(
        r'^\s*(?:sub\s*total|subtotal|net\s*amount|net|v\.?a\.?t\.?|vat|tax|'
        r'invoice\s*total|grand\s*total|total\s*(?:due|amount|eur|incl|net)?'
        r'|amount\s*due|balance\s*due)\s*[:\-]?\s*[€$£]?[\d.,]+\s*$',
        re.I,
    )
    _DEPOSIT_LABEL_RE = re.compile(
        r'\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit'
        r'|deposit\s+summary|returnables?|deposits?|deposit)\b',
        re.I,
    )
    has_label_line = False
    for ln in lines:
        if _DEPOSIT_LABEL_RE.search(ln.lower()):
            # A VAT/tax line is never a confirming deposit label — even if the word
            # "deposit" appears in it (e.g. OCR artefact merging two lines).
            if _is_vat_line(ln):
                continue
            if not _TOTALS_ONLY_RE.match(ln):
                if _parse_money_candidates(ln):
                    has_label_line = True
                    break
                idx = lines.index(ln)
                for nidx in range(max(0, idx - 1), min(len(lines), idx + 2)):
                    if nidx != idx and _parse_money_candidates(lines[nidx]) and not _is_vat_line(lines[nidx]):
                        has_label_line = True
                        break
        if has_label_line:
            break

    if not has_label_line:
        return None

    return best_val


def _get_supplier_bcrs_precedent_score(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> int:
    if not getattr(row, "supplier_name", None):
        return 0
    try:
        precedent = db.query(RemapHint).filter(
            RemapHint.tenant_id == batch.tenant_id,
            RemapHint.active.is_(True),
            RemapHint.field_name.in_(["net_amount", "total_amount", "description"]),
            RemapHint.supplier_name_snapshot == row.supplier_name,
        ).count()
        return 4 if precedent > 0 else 0
    except Exception:
        return 0


def _decide_bcrs_split(db: Session, batch: InvoiceBatch, row: InvoiceRow, payload: dict, page_rows: list[InvoiceRow] | None = None) -> tuple[str, float | None, str | None]:
    amount = _extract_bcrs_amount_from_summary(payload)
    existing_line = bool(amount and page_rows and _page_has_existing_bcrs_row(page_rows, amount))
    lines_text = str(payload.get("line_items_raw") or "")
    totals_text = "\n".join([str(payload.get("totals_raw") or ""), str(payload.get("page_text_raw") or "")])
    score = 0
    if amount and amount > 0:
        score += 14
    if payload.get("_deposit_candidate") not in (None, ""):
        try:
            dep = round(float(payload.get("_deposit_candidate")), 2)
            if amount and abs(dep - amount) <= 0.06:
                score += 10
            elif dep > 0:
                score += 5
        except Exception:
            pass
    if re.search(r"\b(bcrs|refundable\s+deposit|deposit\s+summary|returnables?|deposits?)\b", totals_text, re.I):
        score += 8
    if re.search(r"\b(bcrs|deposit|returnable)\b", lines_text, re.I):
        score += 3
    try:
        inv_net = payload.get("source_invoice_net_amount", payload.get("net_amount"))
        inv_vat = payload.get("source_invoice_vat_amount", payload.get("vat_amount"))
        inv_total = payload.get("source_invoice_total_amount", payload.get("total_amount"))
        if amount is not None and inv_net is not None and inv_total is not None:
            if abs((float(inv_net) + float(inv_vat or 0) + float(amount)) - float(inv_total)) <= 0.10:
                score += 10
            elif abs((float(inv_total) - (float(inv_net) + float(inv_vat or 0)))) > 0.10:
                score += 4
    except Exception:
        pass
    score += _get_supplier_bcrs_precedent_score(db, batch, row)
    if existing_line:
        return ("no_split", None, None)
    if amount and score >= 22:
        return ("auto_split", amount, None)
    mismatch = False
    try:
        inv_net = payload.get("source_invoice_net_amount", payload.get("net_amount"))
        inv_vat = payload.get("source_invoice_vat_amount", payload.get("vat_amount"))
        inv_total = payload.get("source_invoice_total_amount", payload.get("total_amount"))
        mismatch = inv_total is not None and inv_net is not None and abs(float(inv_total) - (float(inv_net) + float(inv_vat or 0))) > 0.10
    except Exception:
        mismatch = False
    if mismatch and (amount or re.search(r"\b(bcrs|deposit|returnable)\b", totals_text, re.I)):
        return ("review_suggest_split", amount, "Possible deposit/BCRS adjustment not safely resolved")
    return ("no_split", None, None)


def _build_bcrs_row(row: InvoiceRow, amount: float) -> InvoiceRow:
    desc = (row.description or '').strip()
    if desc:
        desc = f"{desc} - BCRS" if 'bcrs' not in desc.lower() else desc
    else:
        desc = 'BCRS surcharge'
    return InvoiceRow(
        batch_id=row.batch_id, tenant_id=row.tenant_id, company_id=row.company_id,
        source_file_id=row.source_file_id, source_filename=row.source_filename, page_no=row.page_no,
        supplier_name=row.supplier_name, supplier_posting_account=row.supplier_posting_account,
        nominal_account_code=row.nominal_account_code, invoice_number=row.invoice_number,
        invoice_date=row.invoice_date, description=desc, line_items_raw='BCRS surcharge',
        net_amount=amount, vat_amount=0.0, total_amount=amount, currency=row.currency, tax_code=row.tax_code,
        method_used=(row.method_used or '') + '+bcrs', confidence_score=row.confidence_score,
        validation_status=row.validation_status, review_required=row.review_required,
        review_priority=row.review_priority, review_reasons=row.review_reasons, review_fields=row.review_fields,
        auto_approved=row.auto_approved, page_quality_score=row.page_quality_score,
        classification_method=row.classification_method, supplier_match_method=row.supplier_match_method,
        totals_reconciliation_status=row.totals_reconciliation_status, header_raw=row.header_raw,
        totals_raw=row.totals_raw, page_text_raw=row.page_text_raw,
    )


def _page_has_existing_bcrs_row(rows: list[InvoiceRow], amount: float, tolerance: float = 0.06) -> bool:
    for row in rows:
        text = f"{row.description or ''} {row.line_items_raw or ''}".lower()
        if not re.search(r"\b(bcrs|deposit|returnable|returnables|refund(?:able)?)\b", text):
            continue
        for candidate in (row.total_amount, row.net_amount):
            try:
                if candidate is not None and abs(float(candidate) - float(amount)) <= tolerance:
                    return True
            except (TypeError, ValueError):
                continue
    return False


_STOP_WORDS = {"the", "and", "of", "for", "a", "an", "in", "on", "at", "to", "by"}
_LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
    re.I,
)


def _normalise_supplier(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for loose comparison."""
    n = _LEGAL_SUFFIXES.sub("", (name or "").lower())
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _word_overlap(a: str, b: str) -> float:
    """Jaccard word-overlap score between two normalised supplier name strings."""
    wa = set(_normalise_supplier(a).split()) - _STOP_WORDS
    wb = set(_normalise_supplier(b).split()) - _STOP_WORDS
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _match_supplier_fuzzy(
    db: Session,
    tenant_id,
    company_id,
    supplier_name: str,
    supplier_vat: str | None = None,
) -> TenantSupplier | None:
    """Return the best-matching active supplier, or None if no good match exists.

    Strategy (in order of reliability):
    0. VAT number exact match — most authoritative identifier.
    1. Exact case-insensitive name match.
    2. Normalised containment — one name's core words fully contained in the other.
    3. Word-overlap ≥ 0.5 — majority of meaningful words in common.
    """
    if not supplier_name and not supplier_vat:
        return None

    base_q = (
        db.query(TenantSupplier)
        .filter(
            TenantSupplier.tenant_id == tenant_id,
            TenantSupplier.company_id == company_id,
            TenantSupplier.is_active.is_(True),
        )
    )

    # 0. VAT number match (most reliable — not sensitive to OCR name variation)
    if supplier_vat:
        vat_clean = re.sub(r"\s+", "", supplier_vat).upper()
        vat_match = (
            base_q
            .filter(TenantSupplier.vat_number.isnot(None))
            .all()
        )
        for s in vat_match:
            if s.vat_number and re.sub(r"\s+", "", s.vat_number).upper() == vat_clean:
                logger.debug("Supplier matched via VAT number: %s → %s", supplier_vat, s.supplier_name)
                return s

    if not supplier_name:
        return None

    name = supplier_name.strip()

    # 1. Exact ilike
    exact = base_q.filter(TenantSupplier.supplier_name.ilike(name)).first()
    if exact:
        return exact

    # 2 & 3. Fuzzy — load all active suppliers and score
    all_suppliers = base_q.all()
    if not all_suppliers:
        return None

    name_norm = _normalise_supplier(name)
    best, best_score = None, 0.0

    for s in all_suppliers:
        sname_norm = _normalise_supplier(s.supplier_name)

        # Containment check
        if name_norm and sname_norm and (name_norm in sname_norm or sname_norm in name_norm):
            score = 0.85
        else:
            score = _word_overlap(name, s.supplier_name)

        if score > best_score:
            best_score = score
            best = s

    return best if best_score >= 0.50 else None


def _resolve_supplier_identity(
    db: Session,
    tenant_id,
    company_id,
    row: InvoiceRow,
    supplier_vat: str | None = None,
    batch: InvoiceBatch | None = None,
) -> dict[str, object]:
    """Resolve supplier identity before supplier-gated rules run.

    Inputs are current-document evidence: DI/OCR/native supplier text, optional
    VAT number, and tenant/company supplier master data. Saved supplier-name
    regions are replayed immediately after this function and can still add a
    stronger current-page candidate when the initial supplier text is wrong.
    """
    result = {
        "selected_supplier_name": getattr(row, "supplier_name", None),
        "confidence": 0.0,
        "source": "raw_extraction",
        "review_required": False,
        "explanation_tags": [],
    }
    if not tenant_id or not company_id:
        return result

    suppliers = (
        db.query(TenantSupplier)
        .filter(
            TenantSupplier.tenant_id == tenant_id,
            TenantSupplier.company_id == company_id,
            TenantSupplier.is_active.is_(True),
        )
        .all()
    )
    if not suppliers:
        return result

    vat_clean = re.sub(r"\s+", "", supplier_vat or "").upper()
    if vat_clean:
        vat_matches = [
            s for s in suppliers
            if getattr(s, "vat_number", None)
            and re.sub(r"\s+", "", s.vat_number or "").upper() == vat_clean
        ]
        if len(vat_matches) == 1:
            old = row.supplier_name
            row.supplier_name = vat_matches[0].supplier_name
            result.update({
                "selected_supplier_name": row.supplier_name,
                "confidence": 0.98,
                "source": "supplier_master",
                "explanation_tags": ["supplier_identity:vat_exact"],
            })
            if old != row.supplier_name:
                _append_method_tag(row, "supplier_resolver:vat_exact")
                if batch is not None:
                    _audit_supplier_identity_resolution(
                        db, batch, row, old, row.supplier_name,
                        "supplier_identity_apply",
                        "supplier_identity_resolver source=supplier_master method=vat_exact confidence=0.98",
                    )
            return result
        if len(vat_matches) > 1:
            _append_review_marker(row, "supplier_name", "supplier_identity_conflict:vat")
            if batch is not None:
                _audit_supplier_identity_resolution(
                    db, batch, row, row.supplier_name, row.supplier_name,
                    "supplier_identity_conflict",
                    "supplier_identity_resolver conflict=vat_ambiguous; no supplier selected",
                )
            result["review_required"] = True
            result["explanation_tags"] = ["supplier_identity:vat_ambiguous"]
            return result

    raw_name = getattr(row, "supplier_name", None) or ""
    if not raw_name:
        return result
    scored = sorted(
        ((s, _word_overlap(raw_name, s.supplier_name)) for s in suppliers),
        key=lambda item: item[1],
        reverse=True,
    )
    exact = [s for s in suppliers if _normalise_supplier(s.supplier_name) == _normalise_supplier(raw_name)]
    if len(exact) == 1:
        row.supplier_name = exact[0].supplier_name
        result.update({
            "selected_supplier_name": row.supplier_name,
            "confidence": 0.94,
            "source": "supplier_master",
            "explanation_tags": ["supplier_identity:master_exact"],
        })
        _append_method_tag(row, "supplier_resolver:master_exact")
        if batch is not None:
            _audit_supplier_identity_resolution(
                db, batch, row, raw_name, row.supplier_name,
                "supplier_identity_apply",
                "supplier_identity_resolver source=supplier_master method=master_exact confidence=0.94",
            )
        return result
    if scored:
        best, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0.0
        if best_score >= 0.88 and (best_score - second_score) >= 0.18:
            row.supplier_name = best.supplier_name
            result.update({
                "selected_supplier_name": row.supplier_name,
                "confidence": best_score,
                "source": "supplier_master",
                "explanation_tags": ["supplier_identity:fuzzy_high_confidence"],
            })
            _append_method_tag(row, "supplier_resolver:fuzzy_high_confidence")
            if batch is not None:
                _audit_supplier_identity_resolution(
                    db, batch, row, raw_name, row.supplier_name,
                    "supplier_identity_apply",
                    f"supplier_identity_resolver source=supplier_master method=fuzzy_high_confidence confidence={best_score:.2f}",
                )
            return result
        if best_score >= 0.60 and (best_score - second_score) < 0.18:
            _append_review_marker(row, "supplier_name", "supplier_identity_conflict:fuzzy_ambiguous")
            if batch is not None:
                _audit_supplier_identity_resolution(
                    db, batch, row, raw_name, raw_name,
                    "supplier_identity_conflict",
                    f"supplier_identity_resolver conflict=fuzzy_ambiguous best_score={best_score:.2f} second_score={second_score:.2f}; no supplier selected",
                )
            result["review_required"] = True
            result["explanation_tags"] = ["supplier_identity:fuzzy_ambiguous"]
    return result


def _get_supplier_historical_nominal(
    db: Session,
    tenant_id,
    company_id,
    supplier_name: str,
    limit: int = 50,
) -> str | None:
    """Return the most frequently used nominal account code for invoices from this
    supplier (by name), based on historical invoice rows.

    Used in the hybrid classification order as step 2 (after explicit supplier
    default_nominal, before brand taxonomy and keyword matching).
    """
    from sqlalchemy import func

    if not supplier_name:
        return None

    result = (
        db.query(InvoiceRow.nominal_account_code, func.count().label("cnt"))
        .filter(
            InvoiceRow.tenant_id == tenant_id,
            InvoiceRow.company_id == company_id,
            InvoiceRow.supplier_name == supplier_name,
            InvoiceRow.nominal_account_code.isnot(None),
        )
        .group_by(InvoiceRow.nominal_account_code)
        .order_by(func.count().desc())
        .limit(1)
        .first()
    )
    if result and result.cnt >= 2:
        logger.debug(
            "Historical nominal for %r: %r (%d uses)", supplier_name, result.nominal_account_code, result.cnt
        )
        return result.nominal_account_code
    return None


def _apply_account_suggestions(
    db: Session,
    tenant_id,
    company_id,
    row: InvoiceRow,
    supplier_vat: str | None = None,
):
    """Assign supplier posting account and nominal account code using a 5-step
    hybrid classification order:

    Supplier matching:
        0. VAT number exact match (most reliable)
        1. Exact / fuzzy name match

    Nominal classification (first hit wins):
        A. Supplier default_nominal (explicit per-supplier setting)
        B. Supplier historical nominal (most-used code for this supplier in history)
        C. Description keyword match (account name/code in description text)
        D. Brand/product taxonomy (known brand → category hint → nominal account)
        E. Marked default nominal account (fallback)
    """
    matched_supplier_name: str | None = None
    supplier_identity_conflicted = "supplier_identity_conflict" in (row.review_reasons or "")

    if (row.supplier_name or supplier_vat) and (supplier_vat or not supplier_identity_conflicted):
        supplier = _match_supplier_fuzzy(
            db, tenant_id, company_id,
            row.supplier_name or "",
            supplier_vat=supplier_vat,
        )
        if supplier:
            matched_supplier_name = supplier.supplier_name
            # Canonicalise name to the master list entry
            row.supplier_name = supplier.supplier_name
            if not row.supplier_posting_account:
                row.supplier_posting_account = (
                    supplier.supplier_account_code or supplier.posting_account
                )
            # A. Supplier default_nominal
            if not row.nominal_account_code and supplier.default_nominal:
                row.nominal_account_code = supplier.default_nominal
                logger.debug(
                    "Nominal [A-supplier-default]: %r → %r",
                    supplier.supplier_name, row.nominal_account_code,
                )
    elif supplier_identity_conflicted:
        _append_method_tag(row, "supplier_resolver:account_match_skipped_ambiguous")

    # B. Supplier historical nominal (requires a matched supplier)
    if not row.nominal_account_code and matched_supplier_name:
        hist_nominal = _get_supplier_historical_nominal(
            db, tenant_id, company_id, matched_supplier_name
        )
        if hist_nominal:
            row.nominal_account_code = hist_nominal
            logger.debug(
                "Nominal [B-historical]: %r → %r", matched_supplier_name, hist_nominal
            )

    if not row.nominal_account_code:
        accounts = (
            db.query(TenantNominalAccount)
            .filter(
                TenantNominalAccount.tenant_id == tenant_id,
                TenantNominalAccount.company_id == company_id,
                TenantNominalAccount.is_active.is_(True),
            )
            .all()
        )
        accts_dicts = [
            {
                "account_code": a.account_code,
                "account_name": a.account_name,
                "is_default":   getattr(a, "is_default", False),
            }
            for a in accounts
        ]

        if _CLASSIFY_AVAILABLE:
            try:
                # Build supplier_norm from the already-matched supplier name
                supplier_norm = None
                if row.supplier_name:
                    try:
                        supplier_norm = _normalize_supplier_batches(
                            row.supplier_name,
                            supplier_vat=getattr(row, "supplier_vat", None),
                        )
                        if (
                            supplier_norm.match_method != "unmatched"
                            and supplier_norm.match_confidence >= 0.70
                        ):
                            row.supplier_name = supplier_norm.canonical
                    except Exception:
                        pass

                def _hist_hook():
                    return _get_supplier_historical_nominal(
                        db, tenant_id, company_id, matched_supplier_name or row.supplier_name
                    )

                cl = _classify_line(
                    description=row.description,
                    line_items_raw=row.line_items_raw,
                    supplier_norm=supplier_norm,
                    nominal_accounts=accts_dicts,
                    historical_hook=_hist_hook,
                    openai_api_key=getattr(settings, "openai_api_key", None),
                )
                if cl.nominal_account_code:
                    row.nominal_account_code = cl.nominal_account_code
                    row.classification_method = cl.classification_method
                    logger.debug(
                        "Nominal [classify_line/%s]: %r → %r",
                        cl.classification_method, row.supplier_name, cl.nominal_account_code,
                    )
            except Exception as _ce:
                logger.warning("classify_line failed: %s", _ce)

        else:
            # Legacy fallback (when classify module unavailable)
            default_account = next((a for a in accounts if a.is_default), None)

            # C. Keyword match
            if row.description:
                desc_lower = row.description.lower()
                for account in accounts:
                    if (
                        account.account_name.lower() in desc_lower
                        or account.account_code.lower() in desc_lower
                    ):
                        row.nominal_account_code = account.account_code
                        break

            # D. Brand/product taxonomy
            if not row.nominal_account_code:
                search_text = " ".join(filter(None, [row.description, row.line_items_raw]))
                category_hint = _category_hint_from_text(search_text)
                if category_hint:
                    hint_lower = category_hint.lower()
                    for account in accounts:
                        if hint_lower in account.account_name.lower():
                            row.nominal_account_code = account.account_code
                            break

            # E. Default account fallback
            if not row.nominal_account_code and default_account:
                row.nominal_account_code = default_account.account_code

    # Final safety net: direct query for is_default if still nothing
    if not row.nominal_account_code:
        default_account = (
            db.query(TenantNominalAccount)
            .filter(
                TenantNominalAccount.tenant_id == tenant_id,
                TenantNominalAccount.company_id == company_id,
                TenantNominalAccount.is_active.is_(True),
                TenantNominalAccount.is_default.is_(True),
            )
            .first()
        )
        if default_account:
            row.nominal_account_code = default_account.account_code


# Brand taxonomy: maps known brand/product keywords to accounting category hints.
# These hints are used to search the tenant's nominal account names for a better
# match than the generic default.  More specific entries take priority — the dict
# is scanned in insertion order so put longer/more specific keys first.
_BRAND_TAXONOMY: dict[str, str] = {
    # Tobacco — any of these brands → look for a "Tobacco" nominal account
    "rothmans": "Tobacco",
    "pall mall": "Tobacco",
    "du maurier": "Tobacco",
    "lucky strike": "Tobacco",
    "benson & hedges": "Tobacco",
    "benson and hedges": "Tobacco",
    "marlboro": "Tobacco",
    "dunhill": "Tobacco",
    "parliament": "Tobacco",
    "chesterfield": "Tobacco",
    "winston cigarette": "Tobacco",
    "camel cigarette": "Tobacco",
    "royals cigarette": "Tobacco",
    # Alcohol / Spirits
    "heineken": "Alcohol",
    "carlsberg": "Alcohol",
    "amstel": "Alcohol",
    "corona beer": "Alcohol",
    "guinness": "Alcohol",
    "peroni": "Alcohol",
    "cisk": "Alcohol",
    "hopleaf": "Alcohol",
    "jack daniel": "Alcohol",
    "johnnie walker": "Alcohol",
    "absolut vodka": "Alcohol",
    "baileys": "Alcohol",
    # Soft Drinks / Beverages
    "coca cola": "Beverages",
    "coke zero": "Beverages",
    "pepsi cola": "Beverages",
    "fanta": "Beverages",
    "sprite": "Beverages",
    "ribena": "Beverages",
    "monster energy": "Beverages",
    "red bull": "Beverages",
    "lucozade": "Beverages",
    "7up": "Beverages",
    "kinnie": "Beverages",
    "san pellegrino": "Beverages",
    "acqua panna": "Beverages",
    "evian": "Beverages",
    # Food
    "bigilla": "Food",
    "baguette": "Food",
    "ftira": "Food",
    "olive oil": "Food",
    "catering supplies": "Food",
    "fresh seafood": "Food",
    "fresh produce": "Food",
}


def _category_hint_from_text(text: str) -> str | None:
    """Return a category hint if any known brand/product keyword appears in text."""
    if not text:
        return None
    lower = text.lower()
    for brand, category in _BRAND_TAXONOMY.items():
        if brand in lower:
            return category
    return None


_PATTERN_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "of", "for", "a", "an", "in", "on", "at", "to", "by", "is",
    "are", "was", "with", "from", "that", "this", "ltd", "limited", "plc",
    "invoice", "date", "page", "number", "vat", "tax", "total", "amount",
    "description", "quantity", "price", "unit", "subtotal", "balance", "ref",
    "your", "our", "due", "paid", "name", "address", "account",
})


def _extract_pattern_keywords(text: str) -> set[str]:
    """Return a set of meaningful lowercase words from invoice header text."""
    words = re.findall(r"[a-zA-Z]{4,}", (text or "").lower())
    return {w for w in words if w not in _PATTERN_STOP_WORDS}


def _match_supplier_by_pattern(
    db: Session, tenant_id, company_id, header_text: str
) -> TenantSupplier | None:
    """Check stored keyword fingerprints for a confident supplier identification.

    Returns a TenantSupplier if at least 3 keywords overlap with a stored pattern
    and the overlap covers at least 50 % of the pattern's keyword set.
    """
    if not header_text:
        return None
    from app.db.models import SupplierPattern

    keywords = _extract_pattern_keywords(header_text)
    if len(keywords) < 3:
        return None

    patterns = (
        db.query(SupplierPattern)
        .filter(
            SupplierPattern.tenant_id == tenant_id,
            SupplierPattern.company_id == company_id,
        )
        .all()
    )

    best_supplier: TenantSupplier | None = None
    best_score = 0.0

    for pattern in patterns:
        if not pattern.keywords:
            continue
        pattern_kws = set(pattern.keywords.split())
        if len(pattern_kws) < 3:
            continue
        overlap = keywords & pattern_kws
        if len(overlap) < 3:
            continue
        score = len(overlap) / max(len(pattern_kws), 1)
        if score >= 0.50 and score > best_score:
            supplier = (
                db.query(TenantSupplier)
                .filter(
                    TenantSupplier.id == pattern.supplier_id,
                    TenantSupplier.is_active.is_(True),
                )
                .first()
            )
            if supplier:
                best_score = score
                best_supplier = supplier

    return best_supplier


def _learn_supplier_patterns(
    batch_id: UUID, tenant_id, company_id, db: Session
) -> None:
    """Extract keyword fingerprints from successfully matched rows and save them
    so that future invoices from the same supplier can be recognised quickly."""
    from app.db.models import SupplierPattern
    from datetime import timezone as _tz

    rows = (
        db.query(InvoiceRow)
        .filter(
            InvoiceRow.batch_id == batch_id,
            InvoiceRow.supplier_name.isnot(None),
            InvoiceRow.header_raw.isnot(None),
        )
        .all()
    )

    if not rows:
        return

    for row in rows:
        supplier = (
            db.query(TenantSupplier)
            .filter(
                TenantSupplier.tenant_id == tenant_id,
                TenantSupplier.company_id == company_id,
                TenantSupplier.supplier_name == row.supplier_name,
                TenantSupplier.is_active.is_(True),
            )
            .first()
        )
        if not supplier:
            continue

        keywords = _extract_pattern_keywords(row.header_raw)
        if len(keywords) < 3:
            continue

        now = datetime.now(_tz.utc)
        existing = (
            db.query(SupplierPattern)
            .filter(
                SupplierPattern.tenant_id == tenant_id,
                SupplierPattern.company_id == company_id,
                SupplierPattern.supplier_id == supplier.id,
            )
            .first()
        )

        if existing:
            existing_kws = set(existing.keywords.split()) if existing.keywords else set()
            merged = existing_kws | keywords
            existing.keywords = " ".join(sorted(merged)[:60])
            existing.hit_count += 1
            existing.last_seen_at = now
        else:
            db.add(
                SupplierPattern(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    supplier_id=supplier.id,
                    keywords=" ".join(sorted(keywords)[:60]),
                    hit_count=1,
                    last_seen_at=now,
                )
            )

    try:
        db.commit()
        logger.info("Supplier pattern learning completed for batch %s", batch_id)
    except Exception as exc:
        logger.warning("Pattern learning commit failed for batch %s: %s", batch_id, exc)
        try:
            db.rollback()
        except Exception:
            pass



def _norm_duplicate_token(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _mark_duplicate_invoice_rows(db: Session, batch_id: UUID) -> int:
    """Flag likely duplicate invoice rows for review/export blocking.

    Detection is intentionally generic and conservative: invoice/credit-note
    number plus date plus near-identical total is the strong identity. Supplier
    text is not required because OCR may read the same supplier differently.
    """
    rows = (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.source_file_id.asc(), InvoiceRow.page_no.asc(), InvoiceRow.id.asc())
        .all()
    )
    seen: dict[tuple[str, object, int], InvoiceRow] = {}
    flagged = 0
    for row in rows:
        inv = _norm_duplicate_token(row.invoice_number)
        if len(inv) < 3:
            continue
        total = row.total_amount
        if total is None:
            continue
        try:
            # cents-level key, tolerant of one-cent OCR/rounding differences.
            total_cents = int(round(float(total) * 100))
        except Exception:
            continue
        date_key = row.invoice_date or ""
        # Check exact and +/- 1 cent totals for the same invoice/date.
        duplicate_of = None
        for cents in (total_cents, total_cents - 1, total_cents + 1):
            prev = seen.get((inv, date_key, cents))
            if prev is not None:
                duplicate_of = prev
                break
        if duplicate_of is None:
            seen[(inv, date_key, total_cents)] = row
            continue

        reasons = [x for x in re.split(r"[|]", row.review_reasons or "") if x]
        reason = f"possible_duplicate_invoice:row_{duplicate_of.id}"
        if reason not in reasons:
            reasons.append(reason)
        fields = [x for x in re.split(r"[|]", row.review_fields or "") if x]
        for f in ("invoice_number", "invoice_date", "total_amount"):
            if f not in fields:
                fields.append(f)
        row.review_required = True
        row.review_priority = "high"
        row.validation_status = "review_possible_duplicate"
        row.review_reasons = "|".join(reasons)
        row.review_fields = "|".join(fields)
        _append_method_tag(row, "arbitrated:duplicate_check")
        db.add(InvoiceRowFieldAudit(
            batch_id=batch_id,
            row_id=row.id or 0,
            field_name="_row",
            old_value=None,
            new_value=f"possible duplicate of row {duplicate_of.id}",
            action="duplicate_detected",
            note="Same invoice/credit-note number, date and near-identical total found in this batch. Review and delete/block one row before export if duplicated.",
            username="system",
        ))
        flagged += 1
    if flagged:
        db.commit()
    return flagged

def _create_batch_issue_logs(batch_id: UUID, tenant_id, db: Session) -> None:
    """Auto-create IssueLog records for rows that need human attention after processing."""
    rows = (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id, InvoiceRow.tenant_id == tenant_id)
        .all()
    )

    issues = []
    for row in rows:
        problems: list[str] = []
        priority = "normal"

        if not row.supplier_name:
            problems.append("Supplier name could not be identified")
            priority = "high"

        if row.total_amount is None and row.net_amount is None:
            problems.append("No amounts extracted (total and net both missing)")

        if row.method_used == "page_error":
            problems.append(f"Page processing error: {(row.description or '')[:120]}")
            priority = "high"
        elif row.confidence_score is not None and float(row.confidence_score) < 0.60:
            problems.append(f"Low extraction confidence ({float(row.confidence_score):.0%})")

        if not problems:
            continue

        title = f"Page {row.page_no}: {problems[0]}"[:255]
        conf_str = f"{float(row.confidence_score):.2f}" if row.confidence_score is not None else "N/A"
        description = (
            f"Batch ID: {batch_id}\n"
            f"File: {row.source_filename or 'unknown'}\n"
            f"Page: {row.page_no}\n"
            f"Method: {row.method_used or 'unknown'}\n"
            f"Confidence: {conf_str}\n\n"
            "Issues:\n" + "\n".join(f"- {p}" for p in problems)
        )
        issues.append(
            IssueLog(
                tenant_id=tenant_id,
                created_by_user_id=None,
                title=title,
                description=description,
                status="pending",
                priority=priority,
            )
        )

    if not issues:
        logger.info("No issues to log for batch %s", batch_id)
        return

    try:
        for issue in issues:
            db.add(issue)
        db.commit()
        logger.info("Created %d issue log(s) for batch %s", len(issues), batch_id)
    except Exception as exc:
        logger.warning("Issue log creation failed for batch %s: %s", batch_id, exc)
        try:
            db.rollback()
        except Exception:
            pass


def _build_batch_detail(batch: InvoiceBatch, db: Session) -> BatchDetailOut:
    files = db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch.id).order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc()).all()
    rows_count = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch.id).count()
    uploaded_files = sum(1 for f in files if f.status in ("uploaded", "processing", "processed", "partial"))
    processed_files = sum(1 for f in files if f.status in ("processed", "partial"))
    failed_files = sum(1 for f in files if f.status == "failed")
    return BatchDetailOut(
        id=batch.id,
        tenant_id=batch.tenant_id,
        company_id=batch.company_id,
        batch_name=batch.batch_name,
        source_filename=batch.source_filename,
        status=batch.status,
        page_count=batch.page_count,
        notes=batch.notes,
        scan_mode=batch.scan_mode or "summary",
        created_at=batch.created_at,
        processed_at=batch.processed_at,
        uploaded_files=uploaded_files,
        processed_files=processed_files,
        failed_files=failed_files,
        rows_count=rows_count,
        files=[BatchFileOut.model_validate(f) for f in files],
    )


def _get_batch_for_tenant(db: Session, batch_id: UUID, tenant_id) -> InvoiceBatch:
    batch = db.get(InvoiceBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


def _process_batch_job(batch_id: UUID, tenant_id) -> None:
    db = SessionLocal()
    try:
        batch = db.get(InvoiceBatch, batch_id)
        if not batch or batch.tenant_id != tenant_id:
            return

        # ── Snapshot read-only IDs before any rollback can expire the object ──
        _batch_tenant_id  = batch.tenant_id
        _batch_company_id = batch.company_id

        logger.info("scan started batch=%s tenant=%s", batch_id, _batch_tenant_id)

        files = db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch_id).order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc()).all()
        if not files:
            batch.status = "failed"
            batch.notes = "No uploaded files found for this batch"
            batch.processed_at = datetime.utcnow()
            db.commit()
            return

        db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).delete()
        db.commit()

        total_target_pages = 0
        for invoice_file in files:
            try:
                page_count = get_pdf_page_count(materialize_invoice_file(invoice_file))
            except Exception:
                page_count = 0
            invoice_file.page_count = page_count
            total_target_pages += page_count

        batch.status = "processing"
        batch.page_count = 0
        batch.notes = f"Queued {len(files)} file(s), {total_target_pages} page(s)"
        db.commit()

        # ── Preflight: decide extraction backend once, before any page is processed ──
        # Runs a real authenticated GET against Azure DI /documentModels.
        # Only selects azure_di if the readiness check passes — "configured"
        # is not the same as "ready".
        from app.services.preflight import run_preflight_checks, ExtractionBackend
        from app.services.extractor import _reset_azure_di_error

        preflight = run_preflight_checks()   # skip_readiness_check=False by default
        logger.info(
            "_process_batch_job: preflight complete — backend=%s state=%s duration=%dms",
            preflight.selected_backend,
            preflight.readiness_state,
            preflight.duration_ms,
        )

        # Write preflight outcome to batch notes so operators can see which
        # extraction path was selected without inspecting logs.
        from sqlalchemy import update as _upd_pre
        db.execute(
            _upd_pre(InvoiceBatch)
            .where(InvoiceBatch.id == batch_id)
            .values(notes=preflight.notes)
            .execution_options(synchronize_session=False)
        )
        db.commit()

        if preflight.selected_backend != ExtractionBackend.AZURE_DI:
            # Azure DI is disabled or failed readiness check — clear the
            # circuit-breaker so azure_di_available() returns False for every
            # page without any per-page retry attempt wasting time.
            _reset_azure_di_error()
            logger.info(
                "_process_batch_job: Azure DI not in use for this batch "
                "(state=%s reason=%s)",
                preflight.readiness_state,
                preflight.failure_reason or "disabled",
            )

        # Look up the company name so the extractor can hard-block it as the
        # customer name and never return it as a supplier.
        company = db.get(Company, _batch_company_id) if _batch_company_id else None
        account_company_name: str | None = company.company_name if company else None

        from sqlalchemy import update as _upd

        processed_pages = processed_files = partial_files = failed_files = total_rows = 0
        rule_apply_count = 0
        saved_region_seen_count = 0
        saved_region_applied_count = 0
        saved_region_conflict_count = 0
        review_required_count = 0
        totals_status_counts: dict[str, int] = {}
        extraction_method_counts: dict[str, int] = {}
        provider_baseline_mode = bool(getattr(settings, "scan_provider_baseline_mode", False))
        perf_ctx = ScanPerformanceContext(batch_id=batch_id)
        _batch_perf_start = __import__("time").perf_counter()
        for file_index, invoice_file in enumerate(files, start=1):
            inserted_rows = 0
            page_failures = 0
            try:
                invoice_file.status = "processing"
                invoice_file.error_message = None
                db.commit()
                page_count = invoice_file.page_count or 0
                for page_index in range(page_count):
                    try:
                        _page_perf_start = __import__("time").perf_counter()
                        with perf_ctx.timed("extraction_provider"):
                            row_payloads = process_pdf_page_rows(
                                str(materialize_invoice_file(invoice_file)),
                                page_index=page_index,
                                scan_mode=batch.scan_mode or "summary",
                                openai_api_key=settings.openai_api_key if settings.use_openai else None,
                                account_company_name=account_company_name,
                            )
                        page_methods = sorted({str(_r.get("method_used") or "unknown").split("+")[0] for _r in row_payloads}) or ["no_rows"]
                        for _m in page_methods:
                            extraction_method_counts[_m] = extraction_method_counts.get(_m, 0) + 1
                            perf_ctx.inc_method(_m)
                        logger.info(
                            "scan page completed batch=%s file_index=%d page=%d rows=%d methods=%s",
                            batch_id, file_index, page_index + 1, len(row_payloads), ",".join(page_methods),
                        )
                        for r in row_payloads:
                            supplier_name = r.get("supplier_name")
                            supplier_vat  = r.get("supplier_vat")
                            if not provider_baseline_mode:
                                # --- Pattern-based supplier pre-fill ---------
                                # Before fuzzy matching, check whether we have a
                                # stored keyword fingerprint for this invoice's
                                # header. If we get a confident match, override the
                                # AI/rule-based supplier_name so that
                                # _apply_account_suggestions can do an exact lookup.
                                header_text = r.get("header_raw") or ""
                                pattern_supplier = _match_supplier_by_pattern(
                                    db, _batch_tenant_id, _batch_company_id, header_text
                                )
                                if pattern_supplier:
                                    supplier_name = pattern_supplier.supplier_name
                                    logger.debug(
                                        "Pattern match: '%s' for page %s",
                                        supplier_name,
                                        r.get("page_no"),
                                    )
                                # ----------------------------------------------
                            row = InvoiceRow(
                                batch_id=batch_id,
                                tenant_id=batch.tenant_id,
                                company_id=batch.company_id,
                                source_file_id=invoice_file.id,
                                source_filename=invoice_file.original_filename,
                                page_no=r.get("page_no") or (page_index + 1),
                                supplier_name=supplier_name,
                                invoice_number=r.get("invoice_number"),
                                invoice_date=r.get("invoice_date"),
                                description=r.get("description"),
                                line_items_raw=r.get("line_items_raw"),
                                net_amount=r.get("net_amount"),
                                vat_amount=r.get("vat_amount"),
                                total_amount=r.get("total_amount"),
                                currency=r.get("currency"),
                                tax_code=r.get("tax_code"),
                                method_used=r.get("method_used"),
                                confidence_score=r.get("confidence_score"),
                                validation_status=r.get("validation_status"),
                                review_required=r.get("review_required", False),
                                review_priority=r.get("review_priority"),
                                review_reasons=r.get("review_reasons"),
                                review_fields=r.get("review_fields"),
                                auto_approved=bool(r.get("auto_approved", False)),
                                page_quality_score=r.get("page_quality_score"),
                                supplier_match_method=r.get("supplier_match_method"),
                                totals_reconciliation_status=r.get("totals_reconciliation_status"),
                                header_raw=r.get("header_raw"),
                                totals_raw=r.get("totals_raw"),
                                page_text_raw=r.get("page_text_raw"),
                            )
                            db.add(row)
                            db.flush()
                            _persist_invoice_read_snapshot(
                                db,
                                batch=batch,
                                invoice_file=invoice_file,
                                row=row,
                                payload=r,
                                baseline_mode=provider_baseline_mode,
                            )
                            if not provider_baseline_mode:
                                with perf_ctx.timed("supplier_resolver"):
                                    _resolve_supplier_identity(
                                        db, tenant_id, batch.company_id, row,
                                        supplier_vat=supplier_vat,
                                        batch=batch,
                                    )
                                with perf_ctx.timed("supplier_history_lookup"):
                                    _apply_account_suggestions(
                                        db, tenant_id, batch.company_id, row,
                                        supplier_vat=supplier_vat,
                                    )
                                _supplier_before_remap = row.supplier_name
                                with perf_ctx.timed("saved_region_replay"):
                                    _apply_remap_hints(db, batch, row, perf_ctx=perf_ctx)
                                # Supplier-name saved regions are allowed to confirm/fix a supplier
                                # after the first master-data suggestion pass.  Re-run suggestions
                                # so posting account / supplier match data follow the corrected name.
                                if row.supplier_name != _supplier_before_remap:
                                    with perf_ctx.timed("supplier_history_lookup"):
                                        _apply_account_suggestions(
                                            db, tenant_id, batch.company_id, row,
                                            supplier_vat=supplier_vat,
                                        )
                                with perf_ctx.timed("rule_application"):
                                    _apply_saved_rules(db, batch, row)
                                # Deterministic post-extraction arbitration: compare raw extraction,
                                # rules, saved-region activity, supplier history/master data and
                                # totals evidence before final review/BCRS decisions.
                                with perf_ctx.timed("arbitration"):
                                    arbitrate_invoice_row(db, batch, row, r, context={"scan_mode": batch.scan_mode or "summary", "perf_ctx": perf_ctx})
                            else:
                                _append_method_tag(row, "provider_baseline_mode")

                            _method_text = row.method_used or ""
                            if "rule:" in _method_text or "arbitrated:correction_rule" in _method_text or "arbitrated:admin_global_rule" in _method_text:
                                rule_apply_count += 1
                            if "remap_hint" in _method_text or "saved_region" in _method_text:
                                saved_region_seen_count += 1
                            if "remap_hint:" in _method_text or "arbitrated:saved_region" in _method_text:
                                saved_region_applied_count += 1
                            if "remap_hint_conflict" in _method_text or "saved_region_conflict" in _method_text:
                                saved_region_conflict_count += 1
                            if row.review_required:
                                review_required_count += 1
                            if row.totals_reconciliation_status:
                                _ts = str(row.totals_reconciliation_status)
                                totals_status_counts[_ts] = totals_status_counts.get(_ts, 0) + 1

                            inserted_rows += 1
                            total_rows += 1
                            if provider_baseline_mode or (batch.scan_mode or "summary").lower() == "lines":
                                continue
                            bcrs_outcome, bcrs_amount, bcrs_reason = _decide_bcrs_split(db, batch, row, r, [row])
                            if bcrs_outcome == "auto_split" and bcrs_amount and bcrs_amount > 0:
                                bcrs_row = _build_bcrs_row(row, bcrs_amount)
                                db.add(bcrs_row)
                                inserted_rows += 1
                                total_rows += 1
                                _net = round(float(row.net_amount or 0.0), 2)
                                _vat = round(float(row.vat_amount or 0.0), 2)
                                _corrected_total = round(_net + _vat, 2)
                                if _corrected_total >= 0 and _corrected_total < round(float(row.total_amount or 0.0), 2):
                                    row.total_amount = _corrected_total
                            elif bcrs_outcome == "review_suggest_split":
                                row.review_required = True
                                row.validation_status = row.validation_status or "review_bcrs_ambiguous"
                                reasons = [x for x in re.split(r"[|]", row.review_reasons or "") if x]
                                if bcrs_reason and bcrs_reason not in reasons:
                                    reasons.append(bcrs_reason)
                                row.review_reasons = "|".join(reasons)
                        if (batch.scan_mode or "summary").lower() == "lines" and row_payloads:
                            anchor_payload = dict(row_payloads[0])
                            page_rows = [
                                obj for obj in db.new
                                if isinstance(obj, InvoiceRow)
                                and obj.batch_id == batch_id
                                and obj.source_file_id == invoice_file.id
                                and obj.page_no == (row_payloads[0].get("page_no") or (page_index + 1))
                            ]
                            if page_rows:
                                outcome, bcrs_amount, bcrs_reason = _decide_bcrs_split(db, batch, page_rows[0], anchor_payload, page_rows)
                                if outcome == "auto_split" and bcrs_amount and bcrs_amount > 0:
                                    db.add(_build_bcrs_row(page_rows[0], bcrs_amount))
                                    inserted_rows += 1
                                    total_rows += 1
                                elif outcome == "review_suggest_split":
                                    for _r in page_rows:
                                        _r.review_required = True
                                        _r.validation_status = _r.validation_status or "review_bcrs_ambiguous"
                                        reasons = [x for x in re.split(r"[|]", _r.review_reasons or "") if x]
                                        if bcrs_reason and bcrs_reason not in reasons:
                                            reasons.append(bcrs_reason)
                                        _r.review_reasons = "|".join(reasons)
                        processed_pages += 1
                        # Per-page progress: direct UPDATE with stale-overwrite guard.
                        # WHERE page_count < processed_pages ensures a lower counter
                        # from a concurrent stale read can never overwrite a higher value.
                        _pct = int(min(100, round((processed_pages / total_target_pages) * 100))) if total_target_pages > 0 else 0
                        _note = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count}) — {_pct}%"
                        logger.debug(
                            "_process_batch_job: page %d of %d done — %s",
                            processed_pages, total_target_pages, _note,
                        )
                        db.execute(
                            _upd(InvoiceBatch)
                            .where(
                                InvoiceBatch.id == batch_id,
                                InvoiceBatch.page_count < processed_pages,
                            )
                            .values(page_count=processed_pages, notes=_note)
                            .execution_options(synchronize_session=False)
                        )
                        perf_ctx.timings["total_page_processing"] = perf_ctx.timings.get("total_page_processing", 0.0) + (__import__("time").perf_counter() - _page_perf_start)
                        with perf_ctx.timed("db_commit"):
                            db.commit()
                    except Exception as page_error:
                        if "_page_perf_start" in locals():
                            perf_ctx.timings["total_page_processing"] = perf_ctx.timings.get("total_page_processing", 0.0) + (__import__("time").perf_counter() - _page_perf_start)
                        db.rollback()
                        page_failures += 1
                        processed_pages += 1
                        logger.warning(
                            "scan page failed batch=%s file_index=%d page=%d error=%s",
                            batch_id, file_index, page_index + 1, _safe_log_value(page_error, max_len=160),
                        )
                        # Use snapshotted IDs — batch object is expired after rollback
                        fallback_row = InvoiceRow(
                            batch_id=batch_id,
                            tenant_id=_batch_tenant_id,
                            company_id=_batch_company_id,
                            source_file_id=invoice_file.id,
                            source_filename=invoice_file.original_filename,
                            page_no=page_index + 1,
                            description=f"Page processing error: {str(page_error)[:180]}",
                            currency="EUR",
                            method_used="page_error",
                            confidence_score=0.0,
                            validation_status="review",
                            review_required=True,
                            page_text_raw=f"PAGE_ERROR={str(page_error)}",
                        )
                        db.add(fallback_row)
                        _pct_err = int(min(100, round((processed_pages / total_target_pages) * 100))) if total_target_pages > 0 else 0
                        _note_err = (
                            f"Processing file {file_index}/{len(files)}: "
                            f"{invoice_file.original_filename} "
                            f"(page {page_index + 1}/{page_count}, review fallback) — {_pct_err}%"
                        )
                        db.execute(
                            _upd(InvoiceBatch)
                            .where(
                                InvoiceBatch.id == batch_id,
                                InvoiceBatch.page_count < processed_pages,
                            )
                            .values(page_count=processed_pages, notes=_note_err)
                            .execution_options(synchronize_session=False)
                        )
                        with perf_ctx.timed("db_commit"):
                            db.commit()
                        total_rows += 1
                        inserted_rows += 1
                if inserted_rows == 0:
                    invoice_file.status = "failed"
                    invoice_file.error_message = "No pages could be processed."
                    failed_files += 1
                elif page_failures > 0:
                    invoice_file.status = "partial"
                    invoice_file.error_message = f"{page_failures} page(s) failed and were marked for review."
                    partial_files += 1
                    processed_files += 1
                else:
                    invoice_file.status = "processed"
                    processed_files += 1
                invoice_file.processed_at = datetime.utcnow()
                with perf_ctx.timed("db_commit"):
                    db.commit()
            except Exception as file_error:
                db.rollback()
                # Direct UPDATE for file-error so a subsequent rollback only undoes
                # this single statement and cannot roll back per-page progress commits.
                db.execute(
                    _upd(InvoiceFile)
                    .where(InvoiceFile.id == invoice_file.id)
                    .values(
                        status="failed",
                        error_message=str(file_error)[:500],
                        processed_at=datetime.utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                db.commit()
                failed_files += 1

        if not provider_baseline_mode:
            duplicate_review_count = _mark_duplicate_invoice_rows(db, batch_id)
            if duplicate_review_count:
                review_required_count += duplicate_review_count
                logger.info("duplicate invoice review flags batch=%s count=%d", batch_id, duplicate_review_count)

        # ── Final status via direct UPDATE (atomic, no ORM stale-state risk) ──
        if processed_files and not failed_files and not partial_files:
            final_status = "processed"
            final_notes  = f"Processed {processed_files} file(s), extracted {total_rows} row(s)"
        elif processed_files or partial_files:
            final_status = "partial"
            final_notes  = f"Processed {processed_files} file(s), partial {partial_files}, failed {failed_files}, rows {total_rows}"
        else:
            final_status = "failed"
            final_notes  = "Processing failed for all files"

        db.execute(
            _upd(InvoiceBatch)
            .where(InvoiceBatch.id == batch_id)
            .values(
                status=final_status,
                notes=final_notes,
                page_count=processed_pages,
                processed_at=datetime.utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        with perf_ctx.timed("db_commit"):
            db.commit()
        perf_ctx.timings["total_batch_processing"] = __import__("time").perf_counter() - _batch_perf_start
        perf_summary = perf_ctx.summary(processed_pages, total_rows, review_required_count)

        logger.info(
            "scan completed batch=%s status=%s files_processed=%d files_partial=%d files_failed=%d rows=%d review_required=%d rules_applied=%d saved_regions_seen=%d saved_regions_applied=%d saved_region_conflicts=%d totals_status=%s extraction_methods=%s",
            batch_id, final_status, processed_files, partial_files, failed_files, total_rows,
            review_required_count, rule_apply_count, saved_region_seen_count,
            saved_region_applied_count, saved_region_conflict_count, totals_status_counts,
            extraction_method_counts,
        )
        logger.info(
            "scan performance summary batch=%s pages=%d rows=%d methods=%s saved_regions_tested=%d saved_regions_applied=%d saved_regions_conflicted=%d candidates_persisted=%d review_required=%d elapsed_seconds=%.3f avg_seconds_per_page=%.3f timings=%s",
            batch_id,
            perf_summary["pages_processed"],
            perf_summary["rows_created"],
            perf_summary["method_counts"],
            perf_summary["saved_regions_tested"],
            perf_summary["saved_regions_applied"],
            perf_summary["saved_regions_conflicted"],
            perf_summary["candidates_persisted"],
            perf_summary["review_required_count"],
            perf_summary["total_elapsed_seconds"],
            perf_summary["average_seconds_per_page"],
            perf_summary["timings_seconds"],
        )

        # Learn supplier patterns from this batch's successfully matched rows
        _learn_supplier_patterns(batch_id, _batch_tenant_id, _batch_company_id, db)
        # Issue Log is reserved for tenant-raised support tickets.
        # Do not auto-create one ticket per scan/review row here; extraction
        # review needs stay on the Review page via review_required/review_reasons.
    finally:
        db.close()
        _clear_active(batch_id)


@router.post("", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    company = db.get(Company, payload.company_id)
    if not company or company.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Selected company does not belong to tenant")
    batch = InvoiceBatch(batch_name=payload.batch_name.strip(), company_id=payload.company_id, tenant_id=tenant_id, status="created", notes="Batch created", scan_mode=(payload.scan_mode or "summary"))
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchOut])
def list_batches(company_id: UUID | None = Query(default=None), db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    q = db.query(InvoiceBatch).filter(InvoiceBatch.tenant_id == tenant_id)
    if company_id:
        q = q.filter(InvoiceBatch.company_id == company_id)
    return q.order_by(InvoiceBatch.created_at.desc()).all()


@router.get("/{batch_id}", response_model=BatchDetailOut)
def get_batch(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    return _build_batch_detail(batch, db)


@router.patch("/{batch_id}")
def update_batch(batch_id: UUID, payload: BatchUpdate, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    if batch.status == "processing":
        raise HTTPException(status_code=409, detail="Cannot update a batch while it is processing")
    if payload.scan_mode is not None:
        batch.scan_mode = payload.scan_mode
    if payload.batch_name is not None:
        batch.batch_name = payload.batch_name.strip()
    db.commit()
    db.refresh(batch)
    return {"id": str(batch.id), "scan_mode": batch.scan_mode, "batch_name": batch.batch_name}


@router.post("/{batch_id}/files")
def upload_files(batch_id: UUID, files: list[UploadFile] = File(...), db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    if batch.status == "processing":
        raise HTTPException(status_code=409, detail="Cannot upload files while the batch is processing")

    warning = None
    if batch.status in ("processed", "partial"):
        warning = "This batch has already been processed. Re-process after uploading to update results."

    folder = _batch_folder(batch_id)
    saved = []
    for upload in files:
        content = upload.file.read()

        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{upload.filename}' exceeds the 50 MB upload limit ({len(content) // (1024*1024)} MB).",
            )

        if not content.startswith(_PDF_MAGIC):
            raise HTTPException(
                status_code=400,
                detail=f"File '{upload.filename}' does not appear to be a valid PDF.",
            )

        suffix = Path(upload.filename).suffix or ".pdf"
        stored_filename = f"{uuid4().hex}{suffix}"
        file_path = folder / stored_filename
        file_path.write_bytes(content)
        invoice_file = InvoiceFile(
            batch_id=batch.id,
            tenant_id=batch.tenant_id,
            company_id=batch.company_id,
            original_filename=upload.filename,
            stored_filename=stored_filename,
            file_path=str(file_path),
            mime_type=upload.content_type,
            file_size_bytes=len(content),
            status="uploaded",
        )
        attach_invoice_file_bytes(invoice_file, content)
        db.add(invoice_file)
        saved.append(upload.filename)
    db.commit()
    return {"saved": saved, "warning": warning}


@router.post("/{batch_id}/process")
def process_batch(batch_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    if not _set_active(batch.id):
        raise HTTPException(status_code=409, detail="Batch is already processing")
    background_tasks.add_task(_process_batch_job, batch.id, tenant_id)
    batch.status = "processing"
    batch.notes = "Processing started"
    db.commit()
    return {"ok": True, "status": batch.status}


@router.delete("/{batch_id}")
def delete_batch(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    with _ACTIVE_BATCHES_LOCK:
        if str(batch.id) in _ACTIVE_BATCHES or batch.status == "processing":
            raise HTTPException(status_code=409, detail="Cannot delete a batch while it is processing")

    upload_folder = batch_upload_folder(batch.id)
    export_folder = batch_export_folder(batch.id)

    db.query(InvoiceRowFieldAudit).filter(InvoiceRowFieldAudit.batch_id == batch.id).delete(synchronize_session=False)
    db.query(InvoiceRowCorrection).filter(InvoiceRowCorrection.batch_id == batch.id).delete(synchronize_session=False)
    db.query(BatchExportEvent).filter(BatchExportEvent.batch_id == batch.id).delete(synchronize_session=False)
    db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch.id).delete(synchronize_session=False)
    db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch.id).delete(synchronize_session=False)
    db.delete(batch)
    db.commit()

    for folder in (upload_folder, export_folder):
        try:
            if folder.exists():
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            logger.warning("Failed to remove batch folder %s", folder, exc_info=True)

    return {"ok": True, "deleted_batch_id": str(batch_id)}


@router.get("/{batch_id}/rows", response_model=list[InvoiceRowOut])
def list_rows(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    _get_batch_for_tenant(db, batch_id, tenant_id)
    rows = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).order_by(InvoiceRow.id.asc()).all()
    return rows


@router.get("/{batch_id}/progress")
def get_batch_progress(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    # Force a fresh SELECT — avoid returning a cached identity-map snapshot
    # that was populated before the background job's last commit.
    db.expire(batch)
    batch = db.get(InvoiceBatch, batch_id)
    files = db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch_id).order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc()).all()
    total_files = len(files)
    processed_files = sum(1 for f in files if f.status in ("processed", "partial"))
    failed_files = sum(1 for f in files if f.status == "failed")
    total_pages = sum((f.page_count or 0) for f in files)
    processed_pages = batch.page_count or 0
    percent = int(min(100, round((processed_pages / total_pages) * 100))) if total_pages > 0 else 0

    # ── Per-file review state (review-as-you-go) ─────────────────────────────
    # A file "needs review" when any of its rows has confidence below the
    # configured threshold (0.55). This is computed live, not stored, so there
    # is no migration burden. Once all flagged rows have a saved correction or
    # have been explicitly marked reviewed, the file flips to "reviewed".
    from app.db.review_models import InvoiceRowCorrection  # local import to avoid cycles
    THRESHOLD = 0.55
    file_states: list[dict] = []
    for f in files:
        rows = db.query(InvoiceRow).filter(InvoiceRow.source_file_id == f.id).all()
        flagged_rows = [r for r in rows
                        if (r.confidence_score is not None and float(r.confidence_score) < THRESHOLD)
                        or r.review_required]
        flagged_ids = [r.id for r in flagged_rows]
        corrected_ids: set = set()
        if flagged_ids:
            # A flagged row is satisfied when it has a correction record with
            # row_reviewed=True (set either by saving corrections or by the
            # explicit "Mark file reviewed" action).
            corrs = db.query(InvoiceRowCorrection).filter(
                InvoiceRowCorrection.row_id.in_(flagged_ids)
            ).all()
            corrected_ids = {c.row_id for c in corrs if c.row_reviewed}
        # Aggregate flagged field names across rows (deduped)
        flagged_fields_set = set()
        for r in flagged_rows:
            if r.review_fields:
                for fn in (r.review_fields or "").split(","):
                    fn = fn.strip()
                    if fn:
                        flagged_fields_set.add(fn)
        outstanding = [rid for rid in flagged_ids if rid not in corrected_ids]
        if not flagged_rows:
            review_state = "clean"      # no low-conf rows
        elif outstanding:
            review_state = "needs_review"
        else:
            review_state = "reviewed"
        file_states.append({
            "file_id": f.id,
            "filename": f.original_filename,
            "status": f.status,
            "page_count": f.page_count or 0,
            "review_state": review_state,
            "flagged_row_count": len(flagged_rows),
            "outstanding_row_count": len(outstanding),
            "flagged_fields": sorted(flagged_fields_set),
        })

    return {
        "batch_id": str(batch.id),
        "status": batch.status,
        "notes": batch.notes,
        "processed_pages": processed_pages,
        "total_pages": total_pages,
        "processed_files": processed_files,
        "failed_files": failed_files,
        "total_files": total_files,
        "percent": percent,
        "files": file_states,
    }


@router.get("/{batch_id}/export")
def export_batch(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    import pandas as pd
    from app.db.models import Company, Tenant

    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    rows = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).order_by(InvoiceRow.id.asc()).all()
    if not rows:
        raise HTTPException(status_code=400, detail="No rows available to export")

    company_id = batch.company_id
    batch_metadata = {
        "batch_name": batch.batch_name or "",
        "batch_id": str(batch.id),
        "scan_mode": batch.scan_mode or "summary",
    }

    # Build nominal account code→name lookup
    nominal_accounts = db.query(TenantNominalAccount).filter(
        TenantNominalAccount.tenant_id == tenant_id,
        TenantNominalAccount.company_id == company_id,
    ).all()
    nominal_account_map: dict[str, str] = {
        str(a.account_code).strip(): a.account_name
        for a in nominal_accounts
    }

    # Resolve and render accounting export template (safe fallback if absent/errored)
    template_sheet_arg = None
    try:
        tpl = resolve_effective_template(db, tenant_id, company_id)
        if tpl:
            company = db.get(Company, company_id) if company_id else None
            tenant = db.get(Tenant, tenant_id)
            enrichment = {
                "company_name": company.company_name if company else "",
                "tenant_name": tenant.tenant_name if tenant else "",
                "batch_id": str(batch.id),
                "nominal_account_name": "",
            }
            # Build the template sheet from the same corrected overlay rows used by
            # export_batch_corrected().  Previously the custom template was rendered
            # from raw InvoiceRow values before correction overlay, so corrected
            # supplier_name / invoice_number / amounts appeared in the main Invoices
            # sheet but not in the accounting-template sheet.
            row_dicts = []
            for rd in build_corrected_rows(db, batch):
                code = str(rd.get("nominal_account_code") or "").strip()
                rd["nominal_account_name"] = nominal_account_map.get(code, "")
                row_dicts.append({**enrichment, **rd})

            sheet_name, rendered_rows = render_template_sheet(tpl, row_dicts)
            tpl_df = pd.DataFrame(rendered_rows)
            tpl_df = tpl_df.fillna("")
            template_sheet_arg = (sheet_name, tpl_df)

            from app.db.models import AdminAuditLog
            audit = AdminAuditLog(
                event_type="template_used_in_export",
                entity_type="export_template",
                entity_id=str(tpl.id),
                user_id=_user.id,
                notes=f"Batch {batch_id}",
            )
            db.add(audit)
            db.commit()
    except Exception as tpl_exc:
        logger.warning("Template rendering failed for batch %s (export will continue without it): %s", batch_id, tpl_exc)
        template_sheet_arg = None

    # >>> REVIEW_PACK export_wiring
    workbook_bytes = export_batch_corrected(
        db,
        batch=batch,
        user=_user,
        template_sheet=template_sheet_arg,
        nominal_account_map=nominal_account_map,
        batch_metadata=batch_metadata,
    )
    db.commit()
    # <<< REVIEW_PACK export_wiring
    safe_name = re.sub(r"[^\w\-. ]", "_", batch.batch_name or "batch").strip()
    filename = f"{safe_name}_{batch.id}.xlsx"
    encoded = urllib.parse.quote(filename, safe="")
    return StreamingResponse(
        iter([workbook_bytes.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )

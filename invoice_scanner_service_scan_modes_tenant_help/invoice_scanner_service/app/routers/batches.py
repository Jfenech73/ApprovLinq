from __future__ import annotations

import gc
import logging
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Company, InvoiceBatch, InvoiceFile, InvoiceRow, TenantNominalAccount, TenantSupplier, User
from app.db.session import engine, get_db
from app.routers.auth import current_tenant_id, current_user
from app.schemas import BatchCreate, BatchDetailOut, BatchFileOut, BatchOut, InvoiceRowOut
from app.services.exporter import workbook_from_rows
from app.services.extractor import get_pdf_page_count, process_pdf_page

logger = logging.getLogger("invoice_scanner.batches")
router = APIRouter(prefix="/batches", tags=["batches"])
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_ACTIVE_BATCHES: set[str] = set()
_ACTIVE_BATCHES_LOCK = Lock()


def _batch_folder(batch_id: UUID) -> Path:
    folder = settings.upload_path / str(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


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


def _normalize_supplier_name(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    legal_words = {
        "limited", "ltd", "plc", "llc", "inc", "company", "co", "services", "service",
        "holdings", "holding", "group", "malta", "mt", "trading", "solutions", "solution",
        "international", "intl", "the",
    }
    tokens = [t for t in text.split() if t and t not in legal_words]
    return " ".join(tokens)


def _token_overlap_score(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    base = max(1, min(len(left_tokens), len(right_tokens)))
    return overlap / base


def _normalize_nominal_text(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    stop_words = {"and", "the", "for", "of", "account", "accounts", "nominal", "code", "general", "ledger"}
    tokens = [t for t in text.split() if t and t not in stop_words]
    return " ".join(tokens)


def _set_row_review(row: InvoiceRow, reason: str) -> None:
    reasons = [r for r in re.split(r"\s*\|\s*", (row.validation_status or "")) if r and r.lower() != "ok"]
    if reason not in reasons:
        reasons.append(reason)
    row.review_required = True
    row.validation_status = "review" if not reasons else f"review | {' | '.join(reasons)}"


def _set_row_ok_if_clear(row: InvoiceRow) -> None:
    if row.review_required:
        return
    row.validation_status = "ok"


def _best_nominal_match(db: Session, tenant_id, row: InvoiceRow):
    accounts = (
        db.query(TenantNominalAccount)
        .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.is_active.is_(True))
        .all()
    )
    if not accounts:
        return None, 0.0, "no_nominal_master"

    code_norm = _normalize_nominal_text(row.nominal_account_code)
    desc_norm = _normalize_nominal_text(row.description)
    page_norm = _normalize_nominal_text(row.page_text_raw)

    best_account = None
    best_score = 0.0
    best_type = "none"

    for account in accounts:
        acc_code_norm = _normalize_nominal_text(account.account_code)
        acc_name_norm = _normalize_nominal_text(account.account_name)
        variants = [v for v in {acc_code_norm, acc_name_norm, f"{acc_code_norm} {acc_name_norm}".strip()} if v]
        if not variants:
            continue

        if code_norm and acc_code_norm and code_norm == acc_code_norm:
            return account, 1.0, "exact_code"
        if code_norm and acc_name_norm and code_norm == acc_name_norm:
            return account, 0.99, "exact_name"

        score_parts: list[tuple[float, str]] = []
        for variant in variants:
            if code_norm:
                score_parts.append((SequenceMatcher(None, code_norm, variant).ratio(), "code_similarity"))
                score_parts.append((_token_overlap_score(code_norm, variant), "code_overlap"))
                if variant in code_norm or code_norm in variant:
                    score_parts.append((0.96, "code_contains"))
            if desc_norm:
                if variant in desc_norm:
                    score_parts.append((0.95, "description_contains"))
                score_parts.append((_token_overlap_score(desc_norm, variant) * 0.92, "description_overlap"))
            if page_norm:
                if variant in page_norm:
                    score_parts.append((0.93, "page_contains"))
                score_parts.append((_token_overlap_score(page_norm, variant) * 0.85, "page_overlap"))

        if score_parts:
            score, score_type = max(score_parts, key=lambda item: item[0])
            if score > best_score:
                best_score = score
                best_account = account
                best_type = score_type

    if best_account and best_score >= settings.nominal_match_threshold:
        return best_account, round(best_score, 3), best_type
    return None, round(best_score, 3), best_type


def _best_supplier_match(db: Session, tenant_id, extracted_name: str | None, page_text: str | None = None):
    if not extracted_name and not page_text:
        return None, 0.0, "none"

    suppliers = (
        db.query(TenantSupplier)
        .filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.is_active.is_(True))
        .all()
    )
    if not suppliers:
        return None, 0.0, "none"

    extracted_norm = _normalize_supplier_name(extracted_name)
    page_norm = _normalize_supplier_name(page_text)
    best_supplier = None
    best_score = 0.0
    best_type = "none"

    for supplier in suppliers:
        supplier_norm = _normalize_supplier_name(supplier.supplier_name)
        if not supplier_norm:
            continue

        if extracted_norm and supplier_norm == extracted_norm:
            return supplier, 1.0, "exact_normalized"

        if page_norm and supplier_norm and f" {supplier_norm} " in f" {page_norm} ":
            return supplier, 0.97, "page_contains_supplier"

        score_parts: list[float] = []
        if extracted_norm:
            score_parts.append(SequenceMatcher(None, extracted_norm, supplier_norm).ratio())
            score_parts.append(_token_overlap_score(extracted_norm, supplier_norm))
            if extracted_norm and (supplier_norm in extracted_norm or extracted_norm in supplier_norm):
                score_parts.append(0.94)
        if page_norm:
            if supplier_norm in page_norm:
                score_parts.append(0.95)
            score_parts.append(_token_overlap_score(page_norm, supplier_norm) * 0.9)

        score = max(score_parts) if score_parts else 0.0
        if score > best_score:
            best_score = score
            best_supplier = supplier
            best_type = "fuzzy"

    if best_supplier and best_score >= settings.supplier_match_threshold:
        return best_supplier, round(best_score, 3), best_type
    return None, round(best_score, 3), best_type


def _apply_account_suggestions(db: Session, tenant_id, row: InvoiceRow):
    supplier, score, match_type = _best_supplier_match(db, tenant_id, row.supplier_name, row.page_text_raw)
    suppliers_exist = db.query(TenantSupplier.id).filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.is_active.is_(True)).first() is not None

    supplier_default_nominal = None
    if supplier:
        original_supplier_name = row.supplier_name
        row.supplier_name = supplier.supplier_name
        row.customer_code = supplier.supplier_account_code or supplier.posting_account
        row.supplier_posting_account = supplier.supplier_account_code or supplier.posting_account
        supplier_default_nominal = supplier.default_nominal
        if ("summary_mode" in (row.method_used or "")) and not row.nominal_account_code and supplier.default_nominal:
            row.nominal_account_code = supplier.default_nominal
        row.method_used = f"{row.method_used or 'unknown'}+supplier_master"
        if score < 0.95 or match_type == "fuzzy":
            _set_row_review(row, "supplier_match_needs_review")
        logger.info(
            "Supplier matched from tenant master",
            extra={
                "tenant_id": tenant_id,
                "stage": "supplier_match",
                "status": "matched",
                "supplier_match_score": score,
                "supplier_match_type": match_type,
                "supplier_original_name": original_supplier_name,
                "supplier_final_name": row.supplier_name,
                "review_required": row.review_required,
            },
        )
    else:
        if suppliers_exist and row.supplier_name:
            _set_row_review(row, "supplier_not_matched")
        logger.info(
            "Supplier match not found",
            extra={
                "tenant_id": tenant_id,
                "stage": "supplier_match",
                "status": "no_match",
                "supplier_match_score": score,
                "supplier_match_type": match_type,
                "review_required": row.review_required,
            },
        )

    nominal_account, nominal_score, nominal_type = _best_nominal_match(db, tenant_id, row)
    if nominal_account:
        original_nominal_code = row.nominal_account_code
        row.nominal_account_code = nominal_account.account_code
        row.method_used = f"{row.method_used or 'unknown'}+nominal_master"
        if nominal_score < 0.98 or nominal_type not in {"exact_code", "exact_name"}:
            _set_row_review(row, "nominal_match_needs_review")
        logger.info(
            "Nominal matched from tenant master",
            extra={
                "tenant_id": tenant_id,
                "stage": "nominal_match",
                "status": "matched",
                "nominal_match_score": nominal_score,
                "nominal_match_type": nominal_type,
                "nominal_original_code": original_nominal_code,
                "nominal_final_code": row.nominal_account_code,
                "review_required": row.review_required,
            },
        )
    else:
        nominals_exist = db.query(TenantNominalAccount.id).filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.is_active.is_(True)).first() is not None
        if supplier_default_nominal and not row.nominal_account_code:
            row.nominal_account_code = supplier_default_nominal
            row.method_used = f"{row.method_used or 'unknown'}+supplier_default_nominal"
        elif nominals_exist and (row.nominal_account_code or row.description):
            _set_row_review(row, "nominal_not_matched")
        logger.info(
            "Nominal match not found",
            extra={
                "tenant_id": tenant_id,
                "stage": "nominal_match",
                "status": "no_match",
                "nominal_match_score": nominal_score,
                "nominal_match_type": nominal_type,
                "review_required": row.review_required,
            },
        )

    _set_row_ok_if_clear(row)


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


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return "This file could not be processed."
    if "timeout" in message.lower():
        return "Processing took too long and was stopped for this file."
    return message[:250]


def _process_batch_job(batch_id: UUID, tenant_id, scan_mode: str = "summary") -> None:
    db = SessionLocal()
    logger.info("Batch job started", extra={"batch_id": batch_id, "tenant_id": tenant_id, "stage": "batch", "status": "started"})
    try:
        batch = db.get(InvoiceBatch, batch_id)
        if not batch or batch.tenant_id != tenant_id:
            return
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
                page_count = get_pdf_page_count(invoice_file.file_path)
            except Exception:
                page_count = 0
            invoice_file.page_count = page_count
            total_target_pages += page_count

        batch.status = "processing"
        batch.page_count = 0
        batch.notes = f"Queued {len(files)} file(s), {total_target_pages} page(s) | mode: {scan_mode}"
        db.commit()

        processed_pages = processed_files = partial_files = failed_files = total_rows = 0
        for file_index, invoice_file in enumerate(files, start=1):
            inserted_rows = 0
            page_failures = 0
            file_started = time.perf_counter()
            try:
                invoice_file.status = "processing"
                invoice_file.error_message = None
                db.commit()
                page_count = invoice_file.page_count or 0
                logger.info("File processing started", extra={"batch_id": batch_id, "tenant_id": tenant_id, "file_name": invoice_file.original_filename, "stage": "file", "status": "started"})
                for page_index in range(page_count):
                    try:
                        r = process_pdf_page(invoice_file.file_path, page_index=page_index, openai_api_key=settings.openai_api_key if settings.use_openai else None, scan_mode=scan_mode)
                        result_rows = r.get("rows") if isinstance(r, dict) and isinstance(r.get("rows"), list) else [r]
                        for result_item in result_rows:
                            row = InvoiceRow(
                                batch_id=batch_id,
                                tenant_id=batch.tenant_id,
                                company_id=batch.company_id,
                                source_file_id=invoice_file.id,
                                source_filename=invoice_file.original_filename,
                                page_no=result_item.get("page_no"),
                                supplier_name=result_item.get("supplier_name"),
                                customer_code=result_item.get("customer_code"),
                                invoice_number=result_item.get("invoice_number"),
                                invoice_date=result_item.get("invoice_date"),
                                description=result_item.get("description"),
                                line_items_raw=result_item.get("line_items_raw"),
                                net_amount=result_item.get("net_amount"),
                                vat_amount=result_item.get("vat_amount"),
                                total_amount=result_item.get("total_amount"),
                                currency=result_item.get("currency"),
                                tax_code=result_item.get("tax_code"),
                                method_used=result_item.get("method_used"),
                                confidence_score=result_item.get("confidence_score"),
                                validation_status=result_item.get("validation_status"),
                                review_required=result_item.get("review_required", False),
                                header_raw=result_item.get("header_raw"),
                                totals_raw=result_item.get("totals_raw"),
                                page_text_raw=result_item.get("page_text_raw"),
                            )
                            _apply_account_suggestions(db, tenant_id, row)
                            write_started = time.perf_counter()
                            db.add(row)
                            db.commit()
                            logger.info(
                                "Invoice row saved",
                                extra={
                                    "batch_id": batch_id,
                                    "tenant_id": tenant_id,
                                    "file_name": invoice_file.original_filename,
                                    "page_no": row.page_no,
                                    "stage": "db_write",
                                    "status": "ok",
                                    "duration_ms": int((time.perf_counter() - write_started) * 1000),
                                },
                            )
                            inserted_rows += 1
                            total_rows += 1
                        processed_pages += 1
                        batch.page_count = processed_pages
                        batch.notes = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count})"
                        db.commit()
                    except Exception as page_error:
                        db.rollback()
                        page_failures += 1
                        processed_pages += 1
                        fallback_row = InvoiceRow(
                            batch_id=batch_id,
                            tenant_id=batch.tenant_id,
                            company_id=batch.company_id,
                            source_file_id=invoice_file.id,
                            source_filename=invoice_file.original_filename,
                            page_no=page_index + 1,
                            description=f"Page processing error: {_safe_error_message(page_error)}",
                            currency="EUR",
                            method_used="page_error",
                            confidence_score=0.0,
                            validation_status="review",
                            review_required=True,
                            page_text_raw=f"PAGE_ERROR={_safe_error_message(page_error)}",
                        )
                        db.add(fallback_row)
                        db.commit()
                        total_rows += 1
                        inserted_rows += 1
                        batch.page_count = processed_pages
                        batch.notes = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count}, review fallback)"
                        db.commit()
                        logger.exception(
                            "Page processing failed",
                            extra={"batch_id": batch_id, "tenant_id": tenant_id, "file_name": invoice_file.original_filename, "page_no": page_index + 1, "stage": "page", "status": "failed"},
                        )
                    finally:
                        gc.collect()
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
                db.commit()
                logger.info(
                    "File processing completed",
                    extra={
                        "batch_id": batch_id,
                        "tenant_id": tenant_id,
                        "file_name": invoice_file.original_filename,
                        "stage": "file",
                        "status": invoice_file.status,
                        "duration_ms": int((time.perf_counter() - file_started) * 1000),
                    },
                )
            except Exception as file_error:
                db.rollback()
                invoice_file.status = "failed"
                invoice_file.error_message = _safe_error_message(file_error)
                invoice_file.processed_at = datetime.utcnow()
                db.commit()
                failed_files += 1
                logger.exception(
                    "File processing failed",
                    extra={"batch_id": batch_id, "tenant_id": tenant_id, "file_name": invoice_file.original_filename, "stage": "file", "status": "failed"},
                )
            finally:
                gc.collect()

        batch.page_count = processed_pages
        batch.processed_at = datetime.utcnow()
        if processed_files and not failed_files and not partial_files:
            batch.status = "processed"
            batch.notes = f"Processed {processed_files} file(s), extracted {total_rows} row(s)"
        elif processed_files or partial_files:
            batch.status = "partial"
            batch.notes = f"Processed {processed_files} file(s), partial {partial_files}, failed {failed_files}, rows {total_rows}"
        else:
            batch.status = "failed"
            batch.notes = "Processing failed for all files"
        db.commit()
        logger.info("Batch job completed", extra={"batch_id": batch_id, "tenant_id": tenant_id, "stage": "batch", "status": batch.status})
    finally:
        db.close()
        gc.collect()
        _clear_active(batch_id)


@router.post("", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    company = db.get(Company, payload.company_id)
    if not company or company.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Selected company does not belong to tenant")
    batch = InvoiceBatch(batch_name=payload.batch_name.strip(), company_id=payload.company_id, tenant_id=tenant_id, status="created", notes="Batch created")
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


@router.post("/{batch_id}/files")
def upload_files(batch_id: UUID, files: list[UploadFile] = File(...), db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    folder = _batch_folder(batch_id)
    saved = []
    for upload in files:
        suffix = Path(upload.filename).suffix or ".pdf"
        stored_filename = f"{uuid4().hex}{suffix}"
        file_path = folder / stored_filename
        content = upload.file.read()
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
        db.add(invoice_file)
        saved.append(upload.filename)
    db.commit()
    return {"saved": saved}


@router.post("/{batch_id}/process")
def process_batch(batch_id: UUID, background_tasks: BackgroundTasks, scan_mode: str = Query(default="summary"), db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    if not _set_active(batch.id):
        raise HTTPException(status_code=409, detail="Batch is already processing")
    if scan_mode not in {"summary", "lines"}:
        raise HTTPException(status_code=400, detail="Invalid scan mode")
    background_tasks.add_task(_process_batch_job, batch.id, tenant_id, scan_mode)
    batch.status = "processing"
    batch.notes = f"Processing started | mode: {scan_mode}"
    db.commit()
    return {"ok": True, "status": batch.status}


@router.get("/{batch_id}/rows", response_model=list[InvoiceRowOut])
def list_rows(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    _get_batch_for_tenant(db, batch_id, tenant_id)
    rows = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).order_by(InvoiceRow.id.asc()).all()
    return rows


@router.get("/{batch_id}/progress")
def get_batch_progress(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    files = db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch_id).order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc()).all()
    total_files = len(files)
    processed_files = sum(1 for f in files if f.status in ("processed", "partial"))
    failed_files = sum(1 for f in files if f.status == "failed")
    total_pages = sum((f.page_count or 0) for f in files)
    processed_pages = batch.page_count or 0
    percent = int(min(100, round((processed_pages / total_pages) * 100))) if total_pages > 0 else 0
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
    }


@router.get("/{batch_id}/export")
def export_batch(batch_id: UUID, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    rows = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).order_by(InvoiceRow.id.asc()).all()
    if not rows:
        raise HTTPException(status_code=400, detail="No rows available to export")
    workbook_bytes = workbook_from_rows(rows)
    filename = f"{batch.batch_name.replace(' ', '_') or 'batch'}_{batch.id}.xlsx"
    return StreamingResponse(
        iter([workbook_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

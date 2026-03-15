from __future__ import annotations

import gc
import logging
from datetime import datetime
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
from app.utils.logging_utils import elapsed_ms, get_logger, log_event, now_ms

router = APIRouter(prefix="/batches", tags=["batches"])
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_ACTIVE_BATCHES: set[str] = set()
_ACTIVE_BATCHES_LOCK = Lock()
logger = get_logger("invoice_scanner.batches")


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


def _safe_error_message(message: str | None, limit: int = 255) -> str:
    cleaned = (message or "Unexpected processing error").replace("\n", " ").replace("\r", " ").strip()
    return cleaned[:limit] if cleaned else "Unexpected processing error"


def _cleanup_after_page() -> None:
    gc.collect()


def _apply_account_suggestions(db: Session, tenant_id, row: InvoiceRow):
    if row.supplier_name and not row.supplier_posting_account:
        supplier = (
            db.query(TenantSupplier)
            .filter(TenantSupplier.tenant_id == tenant_id, TenantSupplier.is_active.is_(True))
            .filter(TenantSupplier.supplier_name.ilike(row.supplier_name.strip()))
            .first()
        )
        if supplier:
            row.supplier_posting_account = supplier.supplier_account_code or supplier.posting_account
            if not row.nominal_account_code and supplier.default_nominal:
                row.nominal_account_code = supplier.default_nominal

    if row.description and not row.nominal_account_code:
        accounts = (
            db.query(TenantNominalAccount)
            .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.is_active.is_(True))
            .all()
        )
        text = row.description.lower()
        for account in accounts:
            if account.account_name.lower() in text or account.account_code.lower() in text:
                row.nominal_account_code = account.account_code
                break


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


def _process_batch_job(batch_id: UUID, tenant_id) -> None:
    db = SessionLocal()
    batch_started = now_ms()
    try:
        batch = db.get(InvoiceBatch, batch_id)
        if not batch or batch.tenant_id != tenant_id:
            return

        log_event(logger, logging.INFO, "Batch processing started", batch_id=batch_id, tenant_id=tenant_id)
        files = db.query(InvoiceFile).filter(InvoiceFile.batch_id == batch_id).order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc()).all()
        if not files:
            batch.status = "failed"
            batch.notes = "No uploaded files found for this batch"
            batch.processed_at = datetime.utcnow()
            db.commit()
            log_event(logger, logging.WARNING, "Batch failed - no uploaded files", batch_id=batch_id, tenant_id=tenant_id)
            return

        delete_started = now_ms()
        db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).delete()
        db.commit()
        log_event(logger, logging.INFO, "Existing batch rows cleared", batch_id=batch_id, tenant_id=tenant_id, duration_ms=elapsed_ms(delete_started))

        total_target_pages = 0
        for invoice_file in files:
            try:
                invoice_file.page_count = get_pdf_page_count(invoice_file.file_path)
            except Exception as exc:
                invoice_file.page_count = 0
                invoice_file.error_message = _safe_error_message(f"Page count could not be read: {exc}")
                log_event(logger, logging.WARNING, "Page count read failed", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, error=str(exc)[:180])
            total_target_pages += invoice_file.page_count or 0

        batch.status = "processing"
        batch.page_count = 0
        batch.notes = f"Queued {len(files)} file(s), {total_target_pages} page(s)"
        db.commit()

        processed_pages = processed_files = partial_files = failed_files = total_rows = 0
        for file_index, invoice_file in enumerate(files, start=1):
            file_started = now_ms()
            inserted_rows = 0
            page_failures = 0
            try:
                invoice_file.status = "processing"
                invoice_file.error_message = None
                db.commit()
                page_count = invoice_file.page_count or 0
                log_event(logger, logging.INFO, "File processing started", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, page_count=page_count)

                if page_count <= 0:
                    raise RuntimeError("No PDF pages detected for this file.")

                for page_index in range(page_count):
                    page_started = now_ms()
                    try:
                        r = process_pdf_page(
                            invoice_file.file_path,
                            page_index=page_index,
                            openai_api_key=settings.openai_api_key if settings.use_openai else None,
                        )
                        row = InvoiceRow(
                            batch_id=batch_id,
                            tenant_id=batch.tenant_id,
                            company_id=batch.company_id,
                            source_file_id=invoice_file.id,
                            source_filename=invoice_file.original_filename,
                            page_no=r.get("page_no"),
                            supplier_name=r.get("supplier_name"),
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
                            header_raw=r.get("header_raw"),
                            totals_raw=r.get("totals_raw"),
                            page_text_raw=r.get("page_text_raw"),
                        )
                        db_started = now_ms()
                        _apply_account_suggestions(db, tenant_id, row)
                        db.add(row)
                        db.commit()
                        log_event(logger, logging.INFO, "DB write completed", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, page_no=page_index + 1, duration_ms=elapsed_ms(db_started), supplier_name=(row.supplier_name or '')[:80], invoice_number=(row.invoice_number or '')[:80])
                        inserted_rows += 1
                        total_rows += 1
                        processed_pages += 1
                        batch.page_count = processed_pages
                        batch.notes = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count})"
                        db.commit()
                        log_event(logger, logging.INFO, "Page completed", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, page_no=page_index + 1, duration_ms=elapsed_ms(page_started))
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
                            description=f"Page processing error: {_safe_error_message(str(page_error), 180)}",
                            currency="EUR",
                            method_used="page_error",
                            confidence_score=0.0,
                            validation_status="review",
                            review_required=True,
                            page_text_raw=f"PAGE_ERROR={_safe_error_message(str(page_error), 500)}",
                        )
                        db.add(fallback_row)
                        db.commit()
                        total_rows += 1
                        inserted_rows += 1
                        batch.page_count = processed_pages
                        batch.notes = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count}, review fallback)"
                        db.commit()
                        log_event(logger, logging.WARNING, "Page failed - review fallback created", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, page_no=page_index + 1, duration_ms=elapsed_ms(page_started), error=_safe_error_message(str(page_error), 180))
                    finally:
                        _cleanup_after_page()

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
                log_event(logger, logging.INFO, "File processing completed", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, status=invoice_file.status, inserted_rows=inserted_rows, page_failures=page_failures, duration_ms=elapsed_ms(file_started))
            except Exception as file_error:
                db.rollback()
                invoice_file.status = "failed"
                invoice_file.error_message = _safe_error_message(str(file_error))
                invoice_file.processed_at = datetime.utcnow()
                db.commit()
                failed_files += 1
                log_event(logger, logging.ERROR, "File processing failed", batch_id=batch_id, tenant_id=tenant_id, file_name=invoice_file.original_filename, duration_ms=elapsed_ms(file_started), error=_safe_error_message(str(file_error), 180))
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
        log_event(logger, logging.INFO, "Batch processing completed", batch_id=batch_id, tenant_id=tenant_id, status=batch.status, processed_files=processed_files, partial_files=partial_files, failed_files=failed_files, total_rows=total_rows, duration_ms=elapsed_ms(batch_started))
    except Exception as batch_error:
        db.rollback()
        batch = db.get(InvoiceBatch, batch_id)
        if batch is not None:
            batch.status = "failed"
            batch.processed_at = datetime.utcnow()
            batch.notes = f"Batch failed: {_safe_error_message(str(batch_error), 180)}"
            db.commit()
        log_event(logger, logging.ERROR, "Batch job failed", batch_id=batch_id, tenant_id=tenant_id, error=_safe_error_message(str(batch_error), 180), duration_ms=elapsed_ms(batch_started))
    finally:
        db.close()
        _clear_active(batch_id)
        gc.collect()


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
def process_batch(batch_id: UUID, background_tasks: BackgroundTasks, db: Session = Depends(get_db), tenant_id=Depends(current_tenant_id), _user: User = Depends(current_user)):
    batch = _get_batch_for_tenant(db, batch_id, tenant_id)
    if not _set_active(batch.id):
        raise HTTPException(status_code=409, detail="Batch is already processing")
    background_tasks.add_task(_process_batch_job, batch.id, tenant_id)
    batch.status = "processing"
    batch.notes = "Processing started"
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

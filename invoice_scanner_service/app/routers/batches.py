from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import InvoiceBatch, InvoiceFile, InvoiceRow
from app.db.session import get_db, engine
from app.schemas import BatchCreate, BatchDetailOut, BatchFileOut, BatchOut, InvoiceRowOut
from app.services.exporter import workbook_from_rows
from app.services.extractor import get_pdf_page_count, process_pdf_page

router = APIRouter(prefix="/batches", tags=["batches"])

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_ACTIVE_BATCHES: set[str] = set()
_ACTIVE_BATCHES_LOCK = Lock()


def _batch_folder(batch_id: UUID) -> Path:
    folder = settings.upload_path / str(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _build_batch_detail(batch: InvoiceBatch, db: Session) -> BatchDetailOut:
    files = (
        db.query(InvoiceFile)
        .filter(InvoiceFile.batch_id == batch.id)
        .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
        .all()
    )

    rows_count = db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch.id).count()

    uploaded_files = sum(1 for f in files if f.status in ("uploaded", "processing", "processed", "partial"))
    processed_files = sum(1 for f in files if f.status in ("processed", "partial"))
    failed_files = sum(1 for f in files if f.status == "failed")

    return BatchDetailOut(
        id=batch.id,
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


def _set_active(batch_id: UUID) -> bool:
    key = str(batch_id)
    with _ACTIVE_BATCHES_LOCK:
        if key in _ACTIVE_BATCHES:
            return False
        _ACTIVE_BATCHES.add(key)
        return True


def _clear_active(batch_id: UUID) -> None:
    key = str(batch_id)
    with _ACTIVE_BATCHES_LOCK:
        _ACTIVE_BATCHES.discard(key)


def _process_batch_job(batch_id: UUID) -> None:
    db = SessionLocal()
    try:
        batch = db.get(InvoiceBatch, batch_id)
        if not batch:
            return

        files = (
            db.query(InvoiceFile)
            .filter(InvoiceFile.batch_id == batch_id)
            .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
            .all()
        )
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
        batch.notes = f"Queued {len(files)} file(s), {total_target_pages} page(s)"
        db.commit()

        processed_pages = 0
        processed_files = 0
        partial_files = 0
        failed_files = 0
        total_rows = 0

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
                        r = process_pdf_page(
                            invoice_file.file_path,
                            page_index=page_index,
                            openai_api_key=settings.openai_api_key if settings.use_openai else None,
                        )

                        row = InvoiceRow(
                            batch_id=batch_id,
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
                        db.add(row)
                        db.commit()

                        inserted_rows += 1
                        total_rows += 1
                        processed_pages += 1

                        batch.page_count = processed_pages
                        batch.notes = (
                            f"Processing file {file_index}/{len(files)}: "
                            f"{invoice_file.original_filename} "
                            f"(page {page_index + 1}/{page_count})"
                        )
                        db.commit()

                    except Exception as page_error:
                        db.rollback()
                        page_failures += 1
                        processed_pages += 1

                        fallback_row = InvoiceRow(
                            batch_id=batch_id,
                            source_file_id=invoice_file.id,
                            source_filename=invoice_file.original_filename,
                            page_no=page_index + 1,
                            supplier_name=None,
                            invoice_number=None,
                            invoice_date=None,
                            description=f"Page processing error: {str(page_error)[:180]}",
                            line_items_raw=None,
                            net_amount=None,
                            vat_amount=None,
                            total_amount=None,
                            currency="EUR",
                            tax_code=None,
                            method_used="page_error",
                            confidence_score=0.0,
                            validation_status="review",
                            review_required=True,
                            header_raw=None,
                            totals_raw=None,
                            page_text_raw=f"PAGE_ERROR={str(page_error)}",
                        )
                        db.add(fallback_row)
                        db.commit()

                        total_rows += 1
                        inserted_rows += 1

                        batch.page_count = processed_pages
                        batch.notes = (
                            f"Processing file {file_index}/{len(files)}: "
                            f"{invoice_file.original_filename} "
                            f"(page {page_index + 1}/{page_count}, review fallback)"
                        )
                        db.commit()

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
                    invoice_file.error_message = None
                    processed_files += 1

                invoice_file.processed_at = datetime.utcnow()
                db.commit()

            except Exception as file_error:
                db.rollback()
                invoice_file.status = "failed"
                invoice_file.error_message = str(file_error)
                invoice_file.processed_at = datetime.utcnow()
                db.commit()
                failed_files += 1

        batch.page_count = processed_pages
        batch.processed_at = datetime.utcnow()

        if processed_files and not failed_files and not partial_files:
            batch.status = "processed"
            batch.notes = f"Processed {processed_files} file(s), extracted {total_rows} row(s)"
        elif processed_files or partial_files:
            batch.status = "partial"
            batch.notes = (
                f"Processed {processed_files} file(s), partial {partial_files}, "
                f"failed {failed_files}, rows {total_rows}"
            )
        else:
            batch.status = "failed"
            batch.notes = "Processing failed for all files"

        db.commit()

    finally:
        db.close()
        _clear_active(batch_id)


@router.post("", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)):
    batch = InvoiceBatch(
        batch_name=payload.batch_name.strip(),
        status="created",
        notes="Batch created",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db)):
    return db.query(InvoiceBatch).order_by(InvoiceBatch.created_at.desc()).all()


@router.get("/{batch_id}", response_model=BatchDetailOut)
def get_batch(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _build_batch_detail(batch, db)


@router.get("/{batch_id}/progress")
def get_batch_progress(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    files = (
        db.query(InvoiceFile)
        .filter(InvoiceFile.batch_id == batch_id)
        .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
        .all()
    )

    total_files = len(files)
    processed_files = sum(1 for f in files if f.status in ("processed", "partial"))
    failed_files = sum(1 for f in files if f.status == "failed")
    total_pages = sum((f.page_count or 0) for f in files)
    processed_pages = batch.page_count or 0

    percent = 0
    if total_pages > 0:
        percent = int(min(100, round((processed_pages / total_pages) * 100)))

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


@router.post("/{batch_id}/upload", response_model=BatchDetailOut)
def upload_pdfs(
    batch_id: UUID,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    folder = _batch_folder(batch_id)
    uploaded_names: list[str] = []

    for file in files:
        filename = file.filename or "uploaded.pdf"
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"Only PDF files are supported: {filename}")

        stored_filename = f"{uuid4().hex}_{filename}"
        dest = folder / stored_filename

        with dest.open("wb") as f:
            f.write(file.file.read())

        invoice_file = InvoiceFile(
            batch_id=batch_id,
            original_filename=filename,
            stored_filename=stored_filename,
            file_path=str(dest),
            mime_type=file.content_type,
            status="uploaded",
        )
        db.add(invoice_file)
        uploaded_names.append(filename)

    batch.status = "uploaded"
    batch.page_count = 0
    batch.source_filename = uploaded_names[0] if len(uploaded_names) == 1 else f"{len(uploaded_names)} files"
    batch.notes = f"Uploaded {len(uploaded_names)} file(s)"
    db.commit()

    return _build_batch_detail(batch, db)


@router.post("/{batch_id}/process", response_model=BatchDetailOut)
def process_batch(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    files = (
        db.query(InvoiceFile)
        .filter(InvoiceFile.batch_id == batch_id)
        .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
        .all()
    )
    if not files:
        raise HTTPException(status_code=400, detail="No uploaded files found for this batch")

    if batch.status == "processing":
        return _build_batch_detail(batch, db)

    if not _set_active(batch_id):
        batch.status = "processing"
        batch.notes = "Processing is already running"
        db.commit()
        return _build_batch_detail(batch, db)

    batch.status = "processing"
    batch.processed_at = None
    batch.page_count = 0
    batch.notes = f"Queued {len(files)} file(s) for processing"
    db.commit()

    background_tasks.add_task(_process_batch_job, batch_id)
    return _build_batch_detail(batch, db)


@router.get("/{batch_id}/rows", response_model=list[InvoiceRowOut])
def get_rows(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    return (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.source_file_id.asc().nullsfirst(), InvoiceRow.page_no.asc())
        .all()
    )


@router.get("/{batch_id}/export.xlsx")
def export_batch(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    rows = (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.source_file_id.asc().nullsfirst(), InvoiceRow.page_no.asc())
        .all()
    )

    payload = [
        {
            "source_filename": r.source_filename,
            "page_no": r.page_no,
            "supplier_name": r.supplier_name,
            "invoice_number": r.invoice_number,
            "invoice_date": r.invoice_date.isoformat() if r.invoice_date else None,
            "description": r.description,
            "line_items_raw": r.line_items_raw,
            "net_amount": float(r.net_amount) if r.net_amount is not None else None,
            "vat_amount": float(r.vat_amount) if r.vat_amount is not None else None,
            "total_amount": float(r.total_amount) if r.total_amount is not None else None,
            "currency": r.currency,
            "tax_code": r.tax_code,
            "method_used": r.method_used,
            "confidence_score": float(r.confidence_score) if r.confidence_score is not None else None,
            "validation_status": r.validation_status,
            "review_required": r.review_required,
            "header_raw": r.header_raw,
            "totals_raw": r.totals_raw,
            "page_text_raw": r.page_text_raw,
        }
        for r in rows
    ]

    stream = workbook_from_rows(payload)
    filename = f"{batch.batch_name or batch_id}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
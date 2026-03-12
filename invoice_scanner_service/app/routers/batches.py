from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db.models import InvoiceBatch, InvoiceFile, InvoiceRow
from app.db.session import get_db
from app.schemas import BatchCreate, BatchDetailOut, BatchFileOut, BatchOut, InvoiceRowOut
from app.services.exporter import workbook_from_rows
from app.services.extractor import process_pdf

router = APIRouter(prefix="/batches", tags=["batches"])


_ALLOWED_EXTENSIONS = {".pdf"}


def _sanitize_filename(filename: str) -> str:
    name = Path(filename).name
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip()
    return safe or "invoice.pdf"



def _batch_upload_dir(batch_id: UUID) -> Path:
    path = settings.upload_path / str(batch_id)
    path.mkdir(parents=True, exist_ok=True)
    return path



def _build_batch_detail(batch: InvoiceBatch, db: Session) -> BatchDetailOut:
    files = list(batch.files or [])
    return BatchDetailOut(
        id=batch.id,
        batch_name=batch.batch_name,
        source_filename=batch.source_filename,
        status=batch.status,
        page_count=batch.page_count,
        notes=batch.notes,
        created_at=batch.created_at,
        processed_at=batch.processed_at,
        uploaded_files=len(files),
        processed_files=sum(1 for f in files if f.status == "processed"),
        failed_files=sum(1 for f in files if f.status == "failed"),
        rows_count=db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch.id).count(),
        files=[BatchFileOut.model_validate(f) for f in files],
    )


@router.post("", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)):
    batch = InvoiceBatch(batch_name=payload.batch_name, status="created")
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db)):
    return db.query(InvoiceBatch).order_by(InvoiceBatch.created_at.desc()).all()


@router.get("/{batch_id}", response_model=BatchDetailOut)
def get_batch(batch_id: UUID, db: Session = Depends(get_db)):
    batch = (
        db.query(InvoiceBatch)
        .options(selectinload(InvoiceBatch.files))
        .filter(InvoiceBatch.id == batch_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _build_batch_detail(batch, db)


@router.get("/{batch_id}/files", response_model=list[BatchFileOut])
def list_batch_files(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return (
        db.query(InvoiceFile)
        .filter(InvoiceFile.batch_id == batch_id)
        .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
        .all()
    )


@router.post("/{batch_id}/upload", response_model=BatchDetailOut)
def upload_files(batch_id: UUID, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    batch = (
        db.query(InvoiceBatch)
        .options(selectinload(InvoiceBatch.files))
        .filter(InvoiceBatch.id == batch_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if not files:
        raise HTTPException(status_code=400, detail="No files were provided")

    dest_dir = _batch_upload_dir(batch_id)

    uploaded_names: list[str] = []
    for incoming in files:
        if not incoming.filename:
            raise HTTPException(status_code=400, detail="One of the uploaded files is missing a filename")

        suffix = Path(incoming.filename).suffix.lower()
        if suffix not in _ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type for {incoming.filename}. Only PDF files are supported")

        original_name = _sanitize_filename(incoming.filename)
        stored_name = original_name
        counter = 1
        while (dest_dir / stored_name).exists():
            stem = Path(original_name).stem
            ext = Path(original_name).suffix
            stored_name = f"{stem}_{counter}{ext}"
            counter += 1

        dest = dest_dir / stored_name
        with dest.open("wb") as f:
            f.write(incoming.file.read())

        file_row = InvoiceFile(
            batch_id=batch_id,
            original_filename=original_name,
            stored_filename=stored_name,
            file_path=str(dest),
            mime_type=incoming.content_type,
            status="uploaded",
        )
        db.add(file_row)
        uploaded_names.append(original_name)

    batch.source_filename = ", ".join(uploaded_names[:3]) + (" ..." if len(uploaded_names) > 3 else "")
    batch.status = "uploaded"
    batch.notes = f"Uploaded {len(uploaded_names)} file(s)"
    batch.processed_at = None
    batch.page_count = None

    db.commit()
    db.refresh(batch)
    batch = (
        db.query(InvoiceBatch)
        .options(selectinload(InvoiceBatch.files))
        .filter(InvoiceBatch.id == batch_id)
        .first()
    )
    return _build_batch_detail(batch, db)


@router.post("/{batch_id}/process", response_model=BatchDetailOut)
def process_batch(batch_id: UUID, reset_existing: bool = Query(True), db: Session = Depends(get_db)):
    batch = (
        db.query(InvoiceBatch)
        .options(selectinload(InvoiceBatch.files))
        .filter(InvoiceBatch.id == batch_id)
        .first()
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    files = (
        db.query(InvoiceFile)
        .filter(InvoiceFile.batch_id == batch_id)
        .order_by(InvoiceFile.uploaded_at.asc(), InvoiceFile.id.asc())
        .all()
    )
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded for this batch")

    batch.status = "processing"
    batch.notes = f"Processing {len(files)} file(s)"
    batch.processed_at = None
    db.commit()

    if reset_existing:
        db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).delete()
        db.commit()

    total_pages = 0
    total_rows = 0
    failed_files = 0

    for file_row in files:
        file_path = Path(file_row.file_path)
        if not file_path.exists():
            file_row.status = "failed"
            file_row.error_message = "Uploaded file could not be found on disk"
            file_row.processed_at = datetime.utcnow()
            failed_files += 1
            db.commit()
            continue

        try:
            file_row.status = "processing"
            file_row.error_message = None
            db.commit()

            results = process_pdf(file_path)
            file_row.page_count = len(results)
            file_row.status = "processed"
            file_row.processed_at = datetime.utcnow()

            for r in results:
                row = InvoiceRow(
                    batch_id=batch_id,
                    source_file_id=file_row.id,
                    source_filename=file_row.original_filename,
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
                total_rows += 1

            total_pages += len(results)
            db.commit()
        except Exception as exc:
            db.rollback()
            file_row = db.get(InvoiceFile, file_row.id)
            file_row.status = "failed"
            file_row.error_message = str(exc)[:2000]
            file_row.processed_at = datetime.utcnow()
            failed_files += 1
            db.commit()

    batch = db.get(InvoiceBatch, batch_id)
    batch.page_count = total_pages
    batch.processed_at = datetime.utcnow()
    batch.status = "completed" if failed_files == 0 else ("failed" if failed_files == len(files) else "completed_with_errors")
    batch.notes = f"Processed {len(files) - failed_files}/{len(files)} file(s), {total_rows} extracted row(s), {total_pages} page(s)"
    db.commit()

    batch = (
        db.query(InvoiceBatch)
        .options(selectinload(InvoiceBatch.files))
        .filter(InvoiceBatch.id == batch_id)
        .first()
    )
    return _build_batch_detail(batch, db)


@router.get("/{batch_id}/rows", response_model=list[InvoiceRowOut])
def get_rows(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.source_filename.asc().nulls_last(), InvoiceRow.page_no.asc(), InvoiceRow.id.asc())
        .all()
    )


@router.get("/{batch_id}/export")
@router.get("/{batch_id}/export.xlsx")
def export_batch(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    rows = (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.source_filename.asc().nulls_last(), InvoiceRow.page_no.asc(), InvoiceRow.id.asc())
        .all()
    )

    payload = [{
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
    } for r in rows]

    stream = workbook_from_rows(payload)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", batch.batch_name or str(batch_id)).strip("_") or str(batch_id)
    filename = f"{safe_name}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

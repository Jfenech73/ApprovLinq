from __future__ import annotations
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.db.models import InvoiceBatch, InvoiceRow
from app.schemas import BatchCreate, BatchOut, InvoiceRowOut
from app.services.extractor import process_pdf
from app.services.exporter import workbook_from_rows

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)):
    batch = InvoiceBatch(batch_name=payload.batch_name)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db)):
    return db.query(InvoiceBatch).order_by(InvoiceBatch.created_at.desc()).all()


@router.post("/{batch_id}/upload", response_model=BatchOut)
def upload_pdf(batch_id: UUID, file: UploadFile = File(...), db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    dest = settings.upload_path / f"{batch_id}.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)

    with dest.open("wb") as f:
        f.write(file.file.read())

    batch.source_filename = file.filename
    batch.status = "uploaded"
    batch.notes = "PDF uploaded successfully"
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/{batch_id}/process", response_model=BatchOut)
def process_batch(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    pdf_path = settings.upload_path / f"{batch_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=400, detail="No PDF uploaded for this batch")

    db.query(InvoiceRow).filter(InvoiceRow.batch_id == batch_id).delete()

    results = process_pdf(
        pdf_path,
        openai_api_key=settings.openai_api_key if settings.use_openai else None,
    )

    batch.page_count = len(results)
    batch.status = "processed"
    batch.processed_at = datetime.utcnow()

    populated_rows = 0

    for r in results:
        meaningful = any([
            r.get("supplier_name"),
            r.get("invoice_number"),
            r.get("invoice_date"),
            r.get("line_items_raw"),
            r.get("total_amount") is not None,
            (r.get("page_text_raw") or "").strip(),
        ])
        if meaningful:
            populated_rows += 1

        row = InvoiceRow(
            batch_id=batch_id,
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

    batch.notes = f"Processed {len(results)} pages, populated {populated_rows} rows"
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/{batch_id}/rows", response_model=list[InvoiceRowOut])
def get_rows(batch_id: UUID, db: Session = Depends(get_db)):
    batch = db.get(InvoiceBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return (
        db.query(InvoiceRow)
        .filter(InvoiceRow.batch_id == batch_id)
        .order_by(InvoiceRow.page_no.asc())
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
        .order_by(InvoiceRow.page_no.asc())
        .all()
    )

    payload = [{
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
    filename = f"{batch.batch_name or batch_id}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename=\"{filename}\"'}
    )
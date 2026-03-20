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
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Company, InvoiceBatch, InvoiceFile, InvoiceRow, IssueLog, TenantNominalAccount, TenantSupplier, User
from app.db.session import engine, get_db
from app.routers.auth import current_tenant_id, current_user
from app.schemas import BatchCreate, BatchUpdate, BatchDetailOut, BatchFileOut, BatchOut, InvoiceRowOut
from app.services.exporter import workbook_from_rows
from app.services.extractor import get_pdf_page_count, process_pdf_page_rows

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_PDF_MAGIC = b"%PDF"

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
    db: Session, tenant_id, company_id, supplier_name: str
) -> TenantSupplier | None:
    """Return the best-matching active supplier, or None if no good match exists.

    Strategy (in order):
    1. Exact case-insensitive match.
    2. Normalised containment — one name's core words fully contained in the other.
    3. Word-overlap ≥ 0.5 — majority of meaningful words in common.
    """
    if not supplier_name:
        return None

    name = supplier_name.strip()
    base_q = (
        db.query(TenantSupplier)
        .filter(
            TenantSupplier.tenant_id == tenant_id,
            TenantSupplier.company_id == company_id,
            TenantSupplier.is_active.is_(True),
        )
    )

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


def _apply_account_suggestions(db: Session, tenant_id, company_id, row: InvoiceRow):
    if row.supplier_name:
        supplier = _match_supplier_fuzzy(db, tenant_id, company_id, row.supplier_name)
        if supplier:
            # Canonicalise the name to the list entry so downstream is consistent
            row.supplier_name = supplier.supplier_name
            if not row.supplier_posting_account:
                row.supplier_posting_account = supplier.supplier_account_code or supplier.posting_account
            if not row.nominal_account_code and supplier.default_nominal:
                row.nominal_account_code = supplier.default_nominal

    if row.description and not row.nominal_account_code:
        accounts = (
            db.query(TenantNominalAccount)
            .filter(TenantNominalAccount.tenant_id == tenant_id, TenantNominalAccount.company_id == company_id, TenantNominalAccount.is_active.is_(True))
            .all()
        )
        text = row.description.lower()
        default_account = None
        for account in accounts:
            if account.is_default:
                default_account = account
            if account.account_name.lower() in text or account.account_code.lower() in text:
                row.nominal_account_code = account.account_code
                break

        # If nothing matched by name/code, fall back to the marked default
        if not row.nominal_account_code and default_account:
            row.nominal_account_code = default_account.account_code

    # If still no nominal (e.g. no description), try the default account directly
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
        batch.notes = f"Queued {len(files)} file(s), {total_target_pages} page(s)"
        db.commit()

        # Look up the company name so the extractor can hard-block it as the
        # customer name and never return it as a supplier.
        company = db.get(Company, batch.company_id) if batch.company_id else None
        account_company_name: str | None = company.company_name if company else None

        processed_pages = processed_files = partial_files = failed_files = total_rows = 0
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
                        row_payloads = process_pdf_page_rows(
                            invoice_file.file_path,
                            page_index=page_index,
                            scan_mode=batch.scan_mode or "summary",
                            openai_api_key=settings.openai_api_key if settings.use_openai else None,
                            account_company_name=account_company_name,
                        )
                        for r in row_payloads:
                            # --- Pattern-based supplier pre-fill ---------
                            # Before fuzzy matching, check whether we have a
                            # stored keyword fingerprint for this invoice's
                            # header. If we get a confident match, override the
                            # AI/rule-based supplier_name so that
                            # _apply_account_suggestions can do an exact lookup.
                            header_text = r.get("header_raw") or ""
                            pattern_supplier = _match_supplier_by_pattern(
                                db, tenant_id, batch.company_id, header_text
                            )
                            supplier_name = r.get("supplier_name")
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
                                header_raw=r.get("header_raw"),
                                totals_raw=r.get("totals_raw"),
                                page_text_raw=r.get("page_text_raw"),
                            )
                            _apply_account_suggestions(db, tenant_id, batch.company_id, row)
                            db.add(row)
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
                            description=f"Page processing error: {str(page_error)[:180]}",
                            currency="EUR",
                            method_used="page_error",
                            confidence_score=0.0,
                            validation_status="review",
                            review_required=True,
                            page_text_raw=f"PAGE_ERROR={str(page_error)}",
                        )
                        db.add(fallback_row)
                        db.commit()
                        total_rows += 1
                        inserted_rows += 1
                        batch.page_count = processed_pages
                        batch.notes = f"Processing file {file_index}/{len(files)}: {invoice_file.original_filename} (page {page_index + 1}/{page_count}, review fallback)"
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
            batch.notes = f"Processed {processed_files} file(s), partial {partial_files}, failed {failed_files}, rows {total_rows}"
        else:
            batch.status = "failed"
            batch.notes = "Processing failed for all files"
        db.commit()

        # Learn supplier patterns from this batch's successfully matched rows
        _learn_supplier_patterns(batch_id, tenant_id, batch.company_id, db)
        # Auto-create issue logs for rows needing review
        _create_batch_issue_logs(batch_id, tenant_id, db)
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
    batch_metadata = {
        "batch_name": batch.batch_name or "",
        "batch_id": str(batch.id),
        "scan_mode": batch.scan_mode or "summary",
    }
    # Build a code→name lookup for nominal accounts so the export shows readable names
    company_id = batch.company_id
    nominal_accounts = db.query(TenantNominalAccount).filter(
        TenantNominalAccount.tenant_id == tenant_id,
        TenantNominalAccount.company_id == company_id,
    ).all()
    nominal_account_map: dict[str, str] = {
        str(a.account_code).strip(): a.account_name
        for a in nominal_accounts
    }
    workbook_bytes = workbook_from_rows(
        rows,
        batch_metadata=batch_metadata,
        nominal_account_map=nominal_account_map,
    )
    safe_name = re.sub(r"[^\w\-. ]", "_", batch.batch_name or "batch").strip()
    filename = f"{safe_name}_{batch.id}.xlsx"
    encoded = urllib.parse.quote(filename, safe="")
    return StreamingResponse(
        iter([workbook_bytes.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )

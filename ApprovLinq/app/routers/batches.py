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
from app.services.corrected_exporter import export_batch_corrected
# <<< REVIEW_PACK corrected_export_import
from app.services.extractor import get_pdf_page_count, process_pdf_page_rows
from app.db.review_models import BatchExportEvent, CorrectionRule, InvoiceRowCorrection, InvoiceRowFieldAudit, RemapHint
from app.services.template_render_service import render_template_sheet, resolve_effective_template
from app.utils.storage import batch_upload_folder, batch_export_folder, resolve_upload_path

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_PDF_MAGIC = b"%PDF"

router = APIRouter(prefix="/batches", tags=["batches"])
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_ACTIVE_BATCHES: set[str] = set()
_ACTIVE_BATCHES_LOCK = Lock()


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


def _normalize_rule_value(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _apply_saved_rules(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> None:
    rules = db.query(CorrectionRule).filter(
        CorrectionRule.tenant_id == batch.tenant_id,
        CorrectionRule.active.is_(True),
    )
    if batch.company_id:
        rules = rules.filter((CorrectionRule.company_id == batch.company_id) | (CorrectionRule.company_id.is_(None)))
    else:
        rules = rules.filter(CorrectionRule.company_id.is_(None))
    for rule in rules.order_by(CorrectionRule.id.asc()).all():
        src = _normalize_rule_value(rule.source_pattern)
        if not src:
            continue
        if rule.rule_type == "supplier_alias":
            current = _normalize_rule_value(row.supplier_name)
            if current and current == src and rule.target_value:
                row.supplier_name = rule.target_value
        elif rule.rule_type == "nominal_remap":
            current = _normalize_rule_value(row.nominal_account_code)
            if current and current == src and rule.target_value:
                row.nominal_account_code = rule.target_value



def _apply_remap_hints(db: Session, batch: InvoiceBatch, row: InvoiceRow) -> None:
    """Apply saved RemapHints as extraction guidance.

    Fills a field when:
    - the field is blank, OR
    - the field is listed in review_fields (flagged for review), OR
    - the field value looks obviously suspect (very short / clearly wrong format)

    Preference order: supplier_id match first, normalised name fallback.
    Never overwrites a field that has a solid non-suspect value and is not flagged.
    """
    if not row.supplier_name:
        return

    def _norm(s: str) -> str:
        import re as _re
        n = _re.sub(r"\b(ltd|limited|plc|llc|inc|corp|co|group|trading|holdings|services|solutions)\b",
                    "", (s or "").lower())
        return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]", " ", n)).strip()

    row_norm = _norm(row.supplier_name)
    if not row_norm:
        return

    # Determine which fields are candidates for remap overwrite:
    # blank fields + fields explicitly flagged for review
    _review_fields: set[str] = set()
    if row.review_fields:
        sep = "|" if "|" in (row.review_fields or "") else ","
        _review_fields = {f.strip() for f in row.review_fields.split(sep) if f.strip()}

    _REMAP_FIELDS = (
        "supplier_name", "invoice_number", "invoice_date",
        "net_amount", "vat_amount", "total_amount",
        "nominal_account_code", "description",
    )
    target_fields = {
        f for f in _REMAP_FIELDS
        if not getattr(row, f, None)                     # blank
        or f in _review_fields                            # flagged for review
        or _is_suspect_field_value(f, getattr(row, f, None))  # obviously wrong
    }
    if not target_fields:
        return

    # Query hints: prefer supplier_id match, fall back to name snapshot
    hints_q = db.query(RemapHint).filter(
        RemapHint.tenant_id == batch.tenant_id,
        RemapHint.active.is_(True),
        RemapHint.page_no == row.page_no,
        RemapHint.x.isnot(None),
    )
    all_hints = hints_q.all()
    if not all_hints:
        return

    # Find supplier record for this row (for id-based matching)
    supplier_id: int | None = None
    if row.supplier_name:
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

    # Match by supplier_id first, then by normalised name
    if supplier_id:
        matched = [h for h in all_hints if h.supplier_id == supplier_id]
    else:
        matched = []
    if not matched:
        matched = [
            h for h in all_hints
            if h.supplier_name_snapshot and _norm(h.supplier_name_snapshot) == row_norm
        ]
    if not matched:
        return

    from app.db.models import InvoiceFile as _IF
    from app.utils.storage import resolve_upload_path as _rup
    file_obj = db.get(_IF, row.source_file_id) if row.source_file_id else None
    if not file_obj:
        return
    try:
        pdf_path = str(_rup(file_obj.file_path))
    except Exception:
        return

    for hint in matched:
        if hint.field_name not in target_fields:
            continue
        try:
            from app.routers.review import _read_region_text
            text = _read_region_text(
                pdf_path, hint.page_no or row.page_no,
                float(hint.x), float(hint.y), float(hint.w), float(hint.h),
            )
            if text:
                setattr(row, hint.field_name, text)
                target_fields.discard(hint.field_name)
                logger.debug("RemapHint applied: supplier=%r field=%s → %r",
                             row.supplier_name, hint.field_name, text[:40])
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
    vals = _parse_money_candidates(str(value or ""))
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

    total_amount = _parse_first_money(payload.get('total_amount'))
    net_amount = _parse_first_money(payload.get('net_amount'))
    vat_amount = _parse_first_money(payload.get('vat_amount'))

    lines = _collect_summary_region_lines(payload)
    if not lines:
        return None

    label_re = re.compile(r"\b(bcrs(?:\s+refundable)?(?:\s+deposit)?|refundable\s+deposit|deposit|surcharge)\b", re.I)
    summary_text = "\n".join(lines)
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
        ranked.append((score, f))

    # Pass 1: regex extraction over the whole summary text, useful when OCR collapses rows.
    patterns = [
        re.compile(r"(?is)\bbcrs(?:\s+refundable)?(?:\s+deposit)?\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\brefundable\s+deposit\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
        re.compile(r"(?is)\bdeposit\b[^\d\n€-]{0,24}(?:€\s*)?(-?\d+(?:[.,]\d{2}))"),
    ]
    for pidx, pattern in enumerate(patterns):
        for match in pattern.finditer(summary_text):
            label_span = summary_text[max(0, match.start()-40):min(len(summary_text), match.end()+40)].lower()
            if pidx == 2:
                # Plain "deposit" pattern: accept when the surrounding context contains
                # known summary/totals keywords OR when the matched line itself is in a
                # summary context (catches "Deposit Summary" laid out as separate lines).
                has_context_window = (
                    'bcrs' in label_span
                    or 'summary' in label_span
                    or 'total' in label_span
                    or 'refundable' in label_span
                )
                # Find which collected line this match falls on
                match_line = ""
                pos = 0
                for ln in lines:
                    if pos + len(ln) >= match.start():
                        match_line = ln
                        break
                    pos += len(ln) + 1  # +1 for the "\n" join
                has_summary_line = _is_summary_context(match_line)
                if not has_context_window and not has_summary_line:
                    continue
            raw = match.group(1)
            try:
                val = float(raw.replace(',', '.'))
            except Exception:
                continue
            score = 18 if pidx == 0 else 16 if pidx == 1 else 12
            if total_amount is not None and net_amount is not None and vat_amount is not None:
                if abs((net_amount + vat_amount + val) - total_amount) <= 0.06:
                    score += 12
                elif total_amount > 0 and 0 < val < total_amount:
                    score += 2
            _add_candidate(score, val)

    # Pass 2: line-based scoring within the summary region.
    for idx, line in enumerate(lines):
        low = line.lower()
        label_match = label_re.search(low)
        if not label_match:
            continue
        if _is_body_or_item_context(line) and not _is_summary_context(line):
            continue

        plain_deposit_only = ('deposit' in low and 'bcrs' not in low and 'refundable' not in low and 'surcharge' not in low)
        neighborhood = ' '.join(lines[max(0, idx - 1): min(len(lines), idx + 2)]).lower()
        if plain_deposit_only:
            # Build a list of neighbouring lines that are themselves summary context
            # (not body/item lines). This prevents the "Total" column header in an
            # item table from acting as a false summary signal.
            summary_neighbours = [
                lines[nidx] for nidx in range(max(0, idx - 1), min(len(lines), idx + 2))
                if nidx != idx and _is_summary_context(lines[nidx]) and not _is_body_or_item_context(lines[nidx])
            ]
            has_summary_neighbour = bool(summary_neighbours)
            has_bcrs_nearby = 'bcrs' in neighborhood
            has_summary_keyword = 'summary' in neighborhood
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
                    if 'surcharge' in low:
                        score += 5
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
        r'|deposit\s+summary|deposit\s+surcharge|returnable(?:\s+deposit)?'
        r'|surcharge|deposit)\b',
        re.I,
    )
    has_label_line = False
    for ln in lines:
        if _DEPOSIT_LABEL_RE.search(ln.lower()):
            if not _TOTALS_ONLY_RE.match(ln):
                if _parse_money_candidates(ln):
                    has_label_line = True
                    break
                idx = lines.index(ln)
                for nidx in range(max(0, idx - 1), min(len(lines), idx + 2)):
                    if nidx != idx and _parse_money_candidates(lines[nidx]):
                        has_label_line = True
                        break
        if has_label_line:
            break

    if not has_label_line:
        return None

    return best_val


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

    if row.supplier_name or supplier_vat:
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
                page_count = get_pdf_page_count(resolve_upload_path(invoice_file.file_path))
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
                            str(resolve_upload_path(invoice_file.file_path)),
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
                            supplier_vat  = r.get("supplier_vat")
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
                            _apply_account_suggestions(
                                db, tenant_id, batch.company_id, row,
                                supplier_vat=supplier_vat,
                            )
                            _apply_remap_hints(db, batch, row)
                            _apply_saved_rules(db, batch, row)
                            db.add(row)
                            inserted_rows += 1
                            total_rows += 1
                            bcrs_amount = _extract_bcrs_amount_from_summary(r)
                            if bcrs_amount and bcrs_amount > 0:
                                bcrs_row = _build_bcrs_row(row, bcrs_amount)
                                db.add(bcrs_row)
                                inserted_rows += 1
                                total_rows += 1
                                # Correct the original row total so it no longer includes
                                # the BCRS component.  After the split:
                                #   original.total = net + vat  (BCRS excluded)
                                #   bcrs_row.total = bcrs_amount
                                #   original.total + bcrs_row.total = real invoice total
                                _net = round(float(row.net_amount or 0.0), 2)
                                _vat = round(float(row.vat_amount or 0.0), 2)
                                _corrected_total = round(_net + _vat, 2)
                                if _corrected_total >= 0 and _corrected_total < round(float(row.total_amount or 0.0), 2):
                                    row.total_amount = _corrected_total
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
            # Build per-row enrichment with nominal account names
            row_dicts = []
            for row in rows:
                rd = {col: getattr(row, col, None) for col in row.__table__.columns.keys()}
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

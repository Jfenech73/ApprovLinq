from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as M
from app.db.review_models import InvoiceRowCorrection


class ExportEligibilityError(RuntimeError):
    """Raised when a batch cannot safely export or materialise approved facts."""

    def __init__(self, result: "ExportEligibilityResult"):
        self.result = result
        message = "; ".join(issue.message for issue in result.issues) or "Export is not allowed"
        super().__init__(message)


@dataclass(frozen=True)
class ExportEligibilityIssue:
    code: str
    message: str
    row_id: int | None = None
    file_id: int | None = None
    page_no: int | None = None


@dataclass(frozen=True)
class ExportEligibilityResult:
    eligible: bool
    issues: list[ExportEligibilityIssue] = field(default_factory=list)
    active_row_count: int = 0
    exportable_row_count: int = 0

    @property
    def codes(self) -> set[str]:
        return {issue.code for issue in self.issues}

    def raise_if_blocked(self) -> None:
        if not self.eligible:
            raise ExportEligibilityError(self)


class ExportEligibilityPolicy:
    """Central export/fact/trusted-learning gate.

    Export and fact creation require an approved batch and a clean current scan:
    active rows must be resolved, page errors must not be exported, and failed
    files/pages must be remediated before the batch becomes trusted evidence.
    """

    approved_statuses = frozenset({"approved", "exported"})
    file_error_statuses = frozenset({"failed", "partial"})
    page_error_statuses = frozenset({"failed"})

    def _current_rows_query(self, db: Session, batch: M.InvoiceBatch):
        q = db.query(M.InvoiceRow).filter(M.InvoiceRow.batch_id == batch.id)
        run_id = getattr(batch, "current_scan_run_id", None)
        if run_id is not None:
            q = q.filter(M.InvoiceRow.scan_run_id == run_id)
        return q

    def exportable_rows_query(self, db: Session, batch: M.InvoiceBatch):
        return self._current_rows_query(db, batch).filter(
            M.InvoiceRow.row_status == M.INVOICE_ROW_STATUS_ACTIVE
        )

    def _corrections_by_row(self, db: Session, batch: M.InvoiceBatch) -> dict[int, InvoiceRowCorrection]:
        rows = db.execute(
            select(InvoiceRowCorrection).where(InvoiceRowCorrection.batch_id == batch.id)
        ).scalars().all()
        return {row.row_id: row for row in rows}

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip().lower()

    def row_has_page_error(self, row: M.InvoiceRow) -> bool:
        joined = " ".join(
            self._text(getattr(row, name, None))
            for name in ("method_used", "validation_status", "review_reasons", "page_text_raw")
        )
        return "page_error" in joined or "page_timeout" in joined

    def row_is_unresolved(self, row: M.InvoiceRow, correction: InvoiceRowCorrection | None) -> bool:
        if (getattr(row, "row_status", None) or M.INVOICE_ROW_STATUS_ACTIVE) != M.INVOICE_ROW_STATUS_ACTIVE:
            return False
        if self.row_has_page_error(row):
            return True
        if correction is not None and correction.row_reviewed:
            return False
        if bool(getattr(row, "review_required", False)):
            return True
        if self._text(getattr(row, "review_fields", None)):
            return True
        if self._text(getattr(row, "review_reasons", None)):
            return True
        validation_status = self._text(getattr(row, "validation_status", None))
        return validation_status.startswith("review_") or validation_status in {"review", "failed", "error"}

    def row_is_eligible_for_trusted_learning(
        self,
        db: Session,
        *,
        batch: M.InvoiceBatch,
        row: M.InvoiceRow,
    ) -> bool:
        if (getattr(row, "row_status", None) or M.INVOICE_ROW_STATUS_ACTIVE) != M.INVOICE_ROW_STATUS_ACTIVE:
            return False
        correction = db.get(InvoiceRowCorrection, row.id)
        return not self.row_is_unresolved(row, correction)

    def evaluate(self, db: Session, batch: M.InvoiceBatch, *, require_approval: bool = True) -> ExportEligibilityResult:
        issues: list[ExportEligibilityIssue] = []
        status = self._text(getattr(batch, "status", None) or "created")
        if require_approval and status not in self.approved_statuses:
            issues.append(ExportEligibilityIssue(
                code="batch_not_approved",
                message=f"Batch must be approved before export; current status is {status or 'created'}.",
            ))

        files = db.query(M.InvoiceFile).filter(M.InvoiceFile.batch_id == batch.id).all()
        for file in files:
            if self._text(file.status) in self.file_error_statuses:
                issues.append(ExportEligibilityIssue(
                    code="file_error",
                    message=f"File {file.original_filename or file.id} has status {file.status}.",
                    file_id=file.id,
                ))

        if getattr(batch, "current_scan_run_id", None) is not None:
            pages = db.query(M.ScanJobPage).filter(
                M.ScanJobPage.batch_id == batch.id,
                M.ScanJobPage.scan_run_id == batch.current_scan_run_id,
            ).all()
            for page in pages:
                if self._text(page.status) in self.page_error_statuses:
                    issues.append(ExportEligibilityIssue(
                        code="page_error",
                        message=f"Page {page.page_no} failed during scan processing.",
                        file_id=page.source_file_id,
                        page_no=page.page_no,
                    ))

        rows = self._current_rows_query(db, batch).all()
        active_rows = [
            row for row in rows
            if (getattr(row, "row_status", None) or M.INVOICE_ROW_STATUS_ACTIVE) == M.INVOICE_ROW_STATUS_ACTIVE
        ]
        corrections = self._corrections_by_row(db, batch)
        for row in active_rows:
            if self.row_has_page_error(row):
                issues.append(ExportEligibilityIssue(
                    code="row_page_error",
                    message=f"Row {row.id} is a page-processing error and cannot be exported.",
                    row_id=row.id,
                    page_no=row.page_no,
                ))
                continue
            if self.row_is_unresolved(row, corrections.get(row.id)):
                issues.append(ExportEligibilityIssue(
                    code="row_unresolved",
                    message=f"Row {row.id} still has unresolved review or validation requirements.",
                    row_id=row.id,
                    page_no=row.page_no,
                ))

        if not active_rows:
            issues.append(ExportEligibilityIssue(
                code="no_exportable_rows",
                message="No active rows are available to export.",
            ))

        return ExportEligibilityResult(
            eligible=not issues,
            issues=issues,
            active_row_count=len(active_rows),
            exportable_row_count=len(active_rows),
        )

    def ensure_export_allowed(self, db: Session, batch: M.InvoiceBatch, *, require_approval: bool = True) -> ExportEligibilityResult:
        result = self.evaluate(db, batch, require_approval=require_approval)
        result.raise_if_blocked()
        return result


DEFAULT_EXPORT_ELIGIBILITY_POLICY = ExportEligibilityPolicy()

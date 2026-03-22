from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import (
    AdminAuditLog,
    Company,
    ExportTemplate,
    ExportTemplateColumn,
    Tenant,
    TemplateAssignment,
    User,
)
from app.db.session import get_db
from app.routers.auth import current_user
from app.schemas import (
    AdminAuditLogOut,
    ColumnReorderItem,
    ExportTemplateColumnCreate,
    ExportTemplateColumnOut,
    ExportTemplateColumnUpdate,
    ExportTemplateCreate,
    ExportTemplateDetailOut,
    ExportTemplateOut,
    ExportTemplateUpdate,
    TemplateAssignmentCreate,
    TemplateAssignmentOut,
    TemplatePreviewOut,
)
from app.services.template_render_service import (
    AVAILABLE_FIELDS,
    COLUMN_TYPES,
    render_template_sheet,
    resolve_effective_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/export-templates", tags=["admin-export-templates"])

_SAMPLE_ROW = {
    "source_filename": "sample_invoice.pdf",
    "page_no": 1,
    "supplier_name": "Acme Supplies Ltd",
    "supplier_posting_account": "SUP-001",
    "invoice_number": "INV-2024-001",
    "invoice_date": "2024-03-15",
    "due_date": "2024-04-15",
    "description": "Office supplies and stationery",
    "line_items_raw": "Paper A4 x5 reams; Pens x20",
    "net_amount": 850.00,
    "vat_amount": 195.50,
    "total_amount": 1045.50,
    "currency": "EUR",
    "tax_code": "T1",
    "nominal_account_code": "5000",
    "nominal_account_name": "Office Supplies",
    "validation_status": "ok",
    "review_required": False,
    "company_name": "Demo Company Ltd",
    "tenant_name": "Demo Tenant",
    "batch_id": "batch-00000000-0001",
}


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _audit(
    db: Session,
    event_type: str,
    entity_type: str,
    entity_id: str | None,
    user: User,
    notes: str | None = None,
) -> None:
    entry = AdminAuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user.id,
        notes=notes,
    )
    db.add(entry)
    db.flush()


def _get_template_or_404(db: Session, template_id: UUID) -> ExportTemplate:
    tpl = db.get(ExportTemplate, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


# ── Field Catalog ─────────────────────────────────────────────────────────────

@router.get("/fields", response_model=list[str])
def list_available_fields(_user: User = Depends(require_admin)):
    """Return the catalogue of mappable invoice/database fields."""
    return sorted(AVAILABLE_FIELDS)


# ── Audit Log ─────────────────────────────────────────────────────────────────

@router.get("/audit", response_model=list[AdminAuditLogOut])
def list_audit(
    limit: int = Query(default=100, le=500),
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(AdminAuditLog)
        .filter(AdminAuditLog.entity_type.in_(["export_template", "template_assignment"]))
        .order_by(AdminAuditLog.created_at.desc())
        .limit(limit)
        .all()
    )


# ── Template CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[ExportTemplateOut])
def list_templates(
    search: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    accounting_system: str | None = Query(default=None),
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(ExportTemplate)
    if is_active is not None:
        q = q.filter(ExportTemplate.is_active == is_active)
    if accounting_system:
        q = q.filter(ExportTemplate.accounting_system.ilike(f"%{accounting_system}%"))
    if search:
        term = f"%{search}%"
        q = q.filter(
            ExportTemplate.name.ilike(term) | ExportTemplate.description.ilike(term)
        )
    return q.order_by(ExportTemplate.name.asc()).all()


@router.post("", response_model=ExportTemplateOut)
def create_template(
    payload: ExportTemplateCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = ExportTemplate(
        name=payload.name.strip(),
        description=(payload.description or "").strip() or None,
        accounting_system=(payload.accounting_system or "").strip() or None,
        version_label=payload.version_label.strip() or "v1",
        is_active=payload.is_active,
        is_system_default=payload.is_system_default,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(tpl)
    db.flush()
    _audit(db, "template_created", "export_template", str(tpl.id), user, tpl.name)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.get("/{template_id}", response_model=ExportTemplateDetailOut)
def get_template(
    template_id: UUID,
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = _get_template_or_404(db, template_id)
    return tpl


@router.put("/{template_id}", response_model=ExportTemplateOut)
def update_template(
    template_id: UUID,
    payload: ExportTemplateUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = _get_template_or_404(db, template_id)
    values = payload.model_dump(exclude_unset=True)
    for field, value in values.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(tpl, field, value)
    tpl.updated_by = user.id
    _audit(db, "template_updated", "export_template", str(tpl.id), user, tpl.name)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.patch("/{template_id}/status", response_model=ExportTemplateOut)
def toggle_template_status(
    template_id: UUID,
    is_active: bool,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = _get_template_or_404(db, template_id)
    tpl.is_active = is_active
    tpl.updated_by = user.id
    event = "template_activated" if is_active else "template_deactivated"
    _audit(db, event, "export_template", str(tpl.id), user, tpl.name)
    db.commit()
    db.refresh(tpl)
    return tpl


@router.post("/{template_id}/duplicate", response_model=ExportTemplateDetailOut)
def duplicate_template(
    template_id: UUID,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    original = _get_template_or_404(db, template_id)
    new_tpl = ExportTemplate(
        name=f"{original.name} (copy)",
        description=original.description,
        accounting_system=original.accounting_system,
        version_label=original.version_label,
        is_active=False,
        is_system_default=False,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(new_tpl)
    db.flush()

    for col in original.columns:
        new_col = ExportTemplateColumn(
            template_id=new_tpl.id,
            column_order=col.column_order,
            column_heading=col.column_heading,
            column_type=col.column_type,
            source_field=col.source_field,
            static_value=col.static_value,
            transform_rule=col.transform_rule,
            is_active=col.is_active,
            notes=col.notes,
        )
        db.add(new_col)

    _audit(
        db,
        "template_duplicated",
        "export_template",
        str(new_tpl.id),
        user,
        f"Cloned from {original.id}: {original.name}",
    )
    db.commit()
    db.refresh(new_tpl)
    return new_tpl


# ── Column CRUD ───────────────────────────────────────────────────────────────

@router.post("/{template_id}/columns", response_model=ExportTemplateColumnOut)
def add_column(
    template_id: UUID,
    payload: ExportTemplateColumnCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = _get_template_or_404(db, template_id)

    if payload.column_type == "mapped_field" and payload.source_field not in AVAILABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source_field '{payload.source_field}'. Check /admin/export-templates/fields for the catalogue.",
        )

    col = ExportTemplateColumn(
        template_id=tpl.id,
        column_order=payload.column_order,
        column_heading=payload.column_heading.strip(),
        column_type=payload.column_type,
        source_field=payload.source_field,
        static_value=payload.static_value,
        transform_rule=payload.transform_rule,
        is_active=payload.is_active,
        notes=payload.notes,
    )
    db.add(col)
    tpl.updated_by = user.id
    _audit(db, "column_added", "export_template", str(tpl.id), user, payload.column_heading)
    db.commit()
    db.refresh(col)
    return col


@router.put("/{template_id}/columns/{column_id}", response_model=ExportTemplateColumnOut)
def update_column(
    template_id: UUID,
    column_id: int,
    payload: ExportTemplateColumnUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _get_template_or_404(db, template_id)
    col = db.get(ExportTemplateColumn, column_id)
    if not col or col.template_id != template_id:
        raise HTTPException(status_code=404, detail="Column not found")

    values = payload.model_dump(exclude_unset=True)
    new_type = values.get("column_type", col.column_type)
    new_field = values.get("source_field", col.source_field)
    if new_type == "mapped_field" and new_field and new_field not in AVAILABLE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source_field '{new_field}'.",
        )

    for field, value in values.items():
        setattr(col, field, value)

    db.commit()
    db.refresh(col)
    return col


@router.delete("/{template_id}/columns/{column_id}", status_code=204)
def delete_column(
    template_id: UUID,
    column_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _get_template_or_404(db, template_id)
    col = db.get(ExportTemplateColumn, column_id)
    if not col or col.template_id != template_id:
        raise HTTPException(status_code=404, detail="Column not found")
    db.delete(col)
    db.commit()


@router.put("/{template_id}/columns/reorder", response_model=list[ExportTemplateColumnOut])
def reorder_columns(
    template_id: UUID,
    items: list[ColumnReorderItem],
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _get_template_or_404(db, template_id)
    for item in items:
        col = db.get(ExportTemplateColumn, item.id)
        if col and col.template_id == template_id:
            col.column_order = item.column_order
    db.commit()
    return (
        db.query(ExportTemplateColumn)
        .filter(ExportTemplateColumn.template_id == template_id)
        .order_by(ExportTemplateColumn.column_order.asc())
        .all()
    )


# ── Template Preview ──────────────────────────────────────────────────────────

@router.post("/{template_id}/preview", response_model=TemplatePreviewOut)
def preview_template(
    template_id: UUID,
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = _get_template_or_404(db, template_id)
    try:
        sheet_name, rows = render_template_sheet(tpl, [_SAMPLE_ROW])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Preview failed: {exc}")

    columns = list(rows[0].keys()) if rows else []
    return TemplatePreviewOut(
        sheet_name=sheet_name,
        columns=columns,
        sample_rows=[{k: (str(v) if v is not None else "") for k, v in r.items()} for r in rows],
    )


# ── Assignments ───────────────────────────────────────────────────────────────

@router.get("/assignments", response_model=list[TemplateAssignmentOut])
def list_assignments(
    tenant_id: UUID | None = Query(default=None),
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(TemplateAssignment)
    if tenant_id:
        q = q.filter(TemplateAssignment.tenant_id == tenant_id)
    return q.order_by(TemplateAssignment.tenant_id.asc()).all()


@router.get("/assignments/effective", response_model=TemplateAssignmentOut | None)
def effective_assignment(
    tenant_id: UUID,
    company_id: UUID | None = Query(default=None),
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return the assignment that would be used when exporting for this tenant/company."""
    if company_id:
        assign = (
            db.query(TemplateAssignment)
            .filter(
                TemplateAssignment.tenant_id == tenant_id,
                TemplateAssignment.company_id == company_id,
                TemplateAssignment.is_active.is_(True),
            )
            .first()
        )
        if assign:
            return assign

    assign = (
        db.query(TemplateAssignment)
        .filter(
            TemplateAssignment.tenant_id == tenant_id,
            TemplateAssignment.company_id.is_(None),
            TemplateAssignment.is_active.is_(True),
        )
        .first()
    )
    return assign


@router.post("/assignments", response_model=TemplateAssignmentOut)
def create_assignment(
    payload: TemplateAssignmentCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tpl = db.get(ExportTemplate, payload.template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if not tpl.is_active:
        raise HTTPException(status_code=400, detail="Cannot assign an inactive template")

    if not db.get(Tenant, payload.tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")

    if payload.company_id and not db.get(Company, payload.company_id):
        raise HTTPException(status_code=404, detail="Company not found")

    existing = (
        db.query(TemplateAssignment)
        .filter(
            TemplateAssignment.tenant_id == payload.tenant_id,
            TemplateAssignment.company_id == payload.company_id,
            TemplateAssignment.is_active.is_(True),
        )
        .first()
    )
    if existing:
        existing.template_id = payload.template_id
        existing.assigned_by = user.id
        db.flush()
        _audit(
            db,
            "assignment_updated",
            "template_assignment",
            str(existing.id),
            user,
            f"Template {payload.template_id} → tenant {payload.tenant_id} company {payload.company_id}",
        )
        db.commit()
        db.refresh(existing)
        return existing

    assign = TemplateAssignment(
        template_id=payload.template_id,
        tenant_id=payload.tenant_id,
        company_id=payload.company_id,
        is_active=payload.is_active,
        assigned_by=user.id,
    )
    db.add(assign)
    db.flush()
    _audit(
        db,
        "assignment_created",
        "template_assignment",
        str(assign.id),
        user,
        f"Template {payload.template_id} → tenant {payload.tenant_id} company {payload.company_id}",
    )
    db.commit()
    db.refresh(assign)
    return assign


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_assignment(
    assignment_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    assign = db.get(TemplateAssignment, assignment_id)
    if not assign:
        raise HTTPException(status_code=404, detail="Assignment not found")
    _audit(
        db,
        "assignment_removed",
        "template_assignment",
        str(assign.id),
        user,
        f"Template {assign.template_id} removed from tenant {assign.tenant_id}",
    )
    db.delete(assign)
    db.commit()

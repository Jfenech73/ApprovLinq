# Review/Audit/Learning/Remap Pack — Rollout

## What's in this pack
- `alembic/` — Alembic scaffold + migration `2026_04_11_0001_review_layer.py` (additive only).
- `app/db/review_models.py` — new SQLAlchemy models; attaches additive cols to `InvoiceBatch`.
- `app/services/correction_service.py` — single chokepoint for edits, audits, rules, status.
- `app/services/corrected_exporter.py` — wraps existing `workbook_from_rows`, adds Audit sheet, logs export event.
- `app/routers/review.py` — all new endpoints under `/review`.
- `app/static/review.html` + `app/static/js/review.js` — review workspace UI.
- `tests/test_review_layer.py` — service-level tests using SQLite in-memory.

## 3 small edits you must apply to existing files

### 1. `app/db/__init__.py` — register new models
Append:
```python
from app.db import review_models  # noqa: F401
```

### 2. `app/main.py` — register router and run migration on startup
Add near the other `include_router` calls:
```python
from app.routers import review
app.include_router(review.router)
```
Also append the new tables to the existing startup ALTER block (idempotent) — or
preferably switch startup to: `alembic upgrade head` via subprocess. Both work.

### 3. `app/routers/batches.py` — wire export to corrected exporter
Find your existing export endpoint (the one that calls `workbook_from_rows`) and
replace its body with a call to `corrected_exporter.export_batch_corrected(...)`
when query param `corrected=1` (or unconditionally — recommended). Keep the same
return type (`StreamingResponse`). The corrected exporter sets status to
`exported`, bumps `current_export_version`, and writes a `BatchExportEvent`.

Example diff:
```python
from app.services.corrected_exporter import export_batch_corrected
# ...
buf = export_batch_corrected(db, batch=batch, user=current_user_obj,
                             template_sheet=template_sheet,
                             nominal_account_map=nom_map,
                             batch_metadata=meta)
db.commit()
return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         headers={"Content-Disposition": f'attachment; filename="{batch.batch_name}.xlsx"'})
```

## Migration
```bash
pip install alembic
export DATABASE_URL=postgresql+psycopg://user:pass@host/db
alembic upgrade head
```
Rollback: `alembic downgrade -1` (drops new tables and the 7 added columns).

## Verification checklist
1. Existing scan + extraction unchanged → run an upload, confirm rows appear as before.
2. `GET /review/batches/{id}` returns rows + batch metadata.
3. Edit a description → confirm `invoice_rows.description` is still NULL but `invoice_row_corrections.description` is set.
4. Try to set supplier_name to a non-master value without `force_add` → 422.
5. Save with `save_as_rule_fields=["supplier_name"]` → row in `correction_rules`.
6. Export → file contains an `Audit Changes` sheet; batch status = `exported`; `batch_export_events` row exists with version 1.
7. POST `/review/batches/{id}/reopen` → status `in_review`; export again → version 2.
8. `GET /review/files/{id}/preview?page=1` returns a PNG.
9. POST a remap region → row in `remap_hints`.
10. Run `pytest tests/test_review_layer.py` → all green.

## Key assumptions where reality diverged from the brief
1. **Per-tenant, not per-company** master lists — `TenantSupplier` / `TenantNominalAccount` are tenant-scoped in your schema. Validation is therefore tenant-scoped; rules carry an optional `company_id` for future tightening.
2. **No field-level confidence** in extraction today. Per the brief, no fake values are invented. The UI surfaces the existing CSV `review_fields` from `review_engine.py` as the per-field "needs attention" signal. Schema is ready for true field confidence when extraction provides it.
3. **`InvoiceBatch.status`** is free-text `String(50)`. Lifecycle is enforced in `correction_service.transition_status` rather than via a CHECK constraint, and legacy `"created"` is treated as `"scanned"` at read time — no destructive backfill.
4. **`InvoiceBatch` additive columns** are attached via `review_models._ensure_batch_columns()` so the existing `models.py` is not edited. The Alembic migration adds them in the database.
5. **Remap hints store coordinates only**, not page images. The preview endpoint renders pages on demand via PyMuPDF (already in `requirements.txt`).
6. **Rules apply at write-time as suggestions**, not at extraction-time, in this build. Hooks exist (`CorrectionRule` table) for the extractor to consult later — leaving that activation behind a feature flag is the recommended next step.
7. **Admin gating** uses `User.role == "admin"` (matches your existing model). All other actions (review, edit, approve, export, reopen) are open to any authenticated user, per spec.
8. **No regression risk to existing export** — `corrected_exporter` calls the existing `workbook_from_rows` unchanged; the Audit sheet is appended via `openpyxl.load_workbook` after the fact, so the main sheet output is byte-equivalent when there are zero corrections.

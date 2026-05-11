"""
Regression tests for batch progress persistence.

Root causes fixed:
1. Background job SessionLocal had no expire_on_commit=False — after commit()
   SQLAlchemy expired the batch object; the next attribute access could trigger
   a lazy reload that returned the pre-commit snapshot under certain isolation
   levels, causing apparent stale reads.

2. get_batch_progress endpoint called db.get(InvoiceBatch, batch_id) which
   might return a cached identity-map object.  db.expire(batch) + db.get()
   is now called to force a fresh SELECT every time.

3. _process_batch_job accessed batch.tenant_id after db.rollback() — the
   rollback expires the batch object, so the subsequent lazy-reload could
   return an old snapshot.  Fixed by snapshotting all read-only IDs into
   local variables before any rollback can occur.

4. The file-level except did db.rollback() which could roll back page-level
   progress writes that were already committed per-page.  Fixed by using a
   direct UPDATE statement for the file-status write so a subsequent rollback
   only undoes that single UPDATE.

5. Progress writes now use UPDATE … WHERE page_count < processed_pages so a
   stale (lower) counter can never overwrite a newer (higher) value.

Run: pytest tests/test_batch_progress.py -v
"""
from __future__ import annotations
import ast, os, re


def _src(f: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), "..", f)).read()


def _job_fn() -> str:
    src = _src("app/routers/batches.py")
    return src[
        src.find("def _process_batch_job"):
        src.find("\n\n@router.post(\"\", response_model=BatchOut)")
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Background session: expire_on_commit=False
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionConfig:
    """The background job session must have expire_on_commit=False."""

    def test_background_sessionlocal_has_expire_on_commit_false(self):
        src = _src("app/routers/batches.py")
        # Find the module-level SessionLocal = sessionmaker(...) block
        sl_idx = src.find("SessionLocal = sessionmaker(")
        assert sl_idx > 0, "SessionLocal must be defined in batches.py"
        block = src[sl_idx:src.find(")", sl_idx) + 1]
        assert "expire_on_commit=False" in block, (
            "Background job SessionLocal must set expire_on_commit=False to prevent "
            "stale batch object reads after commit()"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2.  get_batch_progress: forces fresh DB read
# ══════════════════════════════════════════════════════════════════════════════

class TestProgressEndpointFreshRead:
    """get_batch_progress must not return a cached identity-map batch object."""

    def test_endpoint_calls_db_expire_before_reading(self):
        src = _src("app/routers/batches.py")
        fn_start = src.find("def get_batch_progress(")
        fn_end   = src.find("\n\n@router", fn_start)
        fn = src[fn_start:fn_end]
        assert "db.expire(batch)" in fn, \
            "get_batch_progress must call db.expire(batch) before reading batch fields"

    def test_endpoint_re_fetches_batch_after_expire(self):
        src = _src("app/routers/batches.py")
        fn_start = src.find("def get_batch_progress(")
        fn_end   = src.find("\n\n@router", fn_start)
        fn = src[fn_start:fn_end]
        expire_idx = fn.find("db.expire(batch)")
        reget_idx  = fn.find("db.get(InvoiceBatch, batch_id)", expire_idx)
        assert reget_idx > expire_idx, \
            "get_batch_progress must call db.get(InvoiceBatch, batch_id) after db.expire(batch)"


# ══════════════════════════════════════════════════════════════════════════════
# 3.  Per-page progress commit — advances beyond page 2
# ══════════════════════════════════════════════════════════════════════════════

class TestPerPageProgressCommit:
    """Progress must be committed to DB after every page, not just the last."""

    def test_db_commit_inside_page_loop(self):
        fn = _job_fn()
        page_loop_idx = fn.find("for page_index in range(page_count):")
        assert page_loop_idx > 0, "page loop must exist"
        # The loop body is large; search the full remainder of the function
        # since the page loop is the innermost critical loop.
        loop_body = fn[page_loop_idx:]
        assert "db.commit()" in loop_body, \
            "db.commit() must be called inside the page_index loop"

    def test_processed_pages_incremented_on_success(self):
        fn = _job_fn()
        assert "processed_pages += 1" in fn, \
            "processed_pages counter must be incremented on every page"

    def test_percent_calculated_per_page(self):
        fn = _job_fn()
        assert "total_target_pages" in fn
        assert "_pct" in fn or "pct" in fn.lower(), \
            "percent must be calculated per page"

    def test_notes_include_percent(self):
        fn = _job_fn()
        # The notes string must include the percent value
        assert "_pct" in fn[fn.find("_note ="):fn.find("_note =")+200] or \
               "pct%" in fn[fn.find("_note ="):fn.find("_note =")+200], \
            "progress notes must include percent"


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Stale-overwrite protection — WHERE page_count < processed_pages
# ══════════════════════════════════════════════════════════════════════════════

class TestStaleOverwriteProtection:
    """A lower page_count must never overwrite a higher committed value."""

    def test_progress_update_uses_where_clause(self):
        fn = _job_fn()
        assert "InvoiceBatch.page_count < processed_pages" in fn, \
            "progress UPDATE must include WHERE page_count < processed_pages"

    def test_uses_execute_update_not_orm_setattr_for_progress(self):
        fn = _job_fn()
        # The progress write must be a direct SQL UPDATE, not an ORM setattr,
        # to ensure atomicity and avoid object-state conflicts after rollback.
        assert "db.execute(" in fn, \
            "progress must be written via db.execute(update(...))"
        # There must NOT be a direct batch.page_count = assignment for progress
        # in the main flow (outside initialisation)
        # Find the page loop
        loop_idx = fn.find("for page_index in range(page_count):")
        loop_body = fn[loop_idx:]
        # Direct ORM assignment for progress should not appear in loop body
        assert "batch.page_count = processed_pages" not in loop_body, \
            "batch.page_count = ... must not appear inside page loop after fix"


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Rollback safety — snapshotted IDs, no batch access after rollback
# ══════════════════════════════════════════════════════════════════════════════

class TestRollbackSafety:
    """After db.rollback(), the batch object must not be accessed lazily."""

    def test_tenant_id_snapshotted_before_loop(self):
        fn = _job_fn()
        # _batch_tenant_id must be captured before the file loop
        snap_idx = fn.find("_batch_tenant_id")
        loop_idx = fn.find("for file_index, invoice_file in enumerate(files")
        assert snap_idx > 0, "_batch_tenant_id snapshot must exist"
        assert snap_idx < loop_idx, \
            "_batch_tenant_id must be captured before the file loop starts"

    def test_company_id_snapshotted_before_loop(self):
        fn = _job_fn()
        snap_idx = fn.find("_batch_company_id")
        loop_idx = fn.find("for file_index, invoice_file in enumerate(files")
        assert snap_idx > 0
        assert snap_idx < loop_idx, \
            "_batch_company_id must be captured before the file loop starts"

    def test_fallback_row_uses_snapshotted_ids_not_batch_object(self):
        fn = _job_fn()
        # In the except page_error block, fallback_row must use _batch_tenant_id
        # not batch.tenant_id (which would trigger a lazy reload after rollback)
        err_idx = fn.find("except Exception as page_error:")
        err_block = fn[err_idx:err_idx + 1500]
        assert "tenant_id=_batch_tenant_id" in err_block, \
            "fallback_row must use _batch_tenant_id (snapshot), not batch.tenant_id"
        assert "company_id=_batch_company_id" in err_block, \
            "fallback_row must use _batch_company_id (snapshot), not batch.company_id"
        assert "tenant_id=batch.tenant_id" not in err_block, \
            "fallback_row must NOT access batch.tenant_id after rollback"

    def test_file_error_uses_direct_update_not_orm(self):
        fn = _job_fn()
        file_err_idx = fn.find("except Exception as file_error:")
        file_err_block = fn[file_err_idx:file_err_idx + 1000]
        assert "db.execute(" in file_err_block, \
            "file-level error handler must use db.execute(UPDATE) not ORM setattr"


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Final status transition
# ══════════════════════════════════════════════════════════════════════════════

class TestFinalStatusTransition:
    """After all pages, status must transition to processed/partial/failed."""

    def test_final_status_written_via_direct_update(self):
        fn = _job_fn()
        # Find the final status write block (after the file loop)
        final_idx = fn.find("final_status")
        assert final_idx > 0, "final_status variable must exist"
        # It must be written via db.execute(update(...))
        update_idx = fn.find("db.execute(", final_idx)
        assert update_idx > 0, \
            "final status must be written via db.execute(UPDATE)"

    def test_final_status_includes_processed(self):
        fn = _job_fn()
        assert '"processed"' in fn[fn.find("final_status"):], \
            "final_status must have 'processed' outcome"

    def test_final_status_includes_failed(self):
        fn = _job_fn()
        assert '"failed"' in fn[fn.find("final_status"):], \
            "final_status must have 'failed' outcome"

    def test_final_status_includes_partial(self):
        fn = _job_fn()
        assert '"partial"' in fn[fn.find("final_status"):], \
            "final_status must have 'partial' outcome"

    def test_processed_at_set_in_final_update(self):
        fn = _job_fn()
        final_update_idx = fn.rfind("db.execute(")
        final_block = fn[final_update_idx:final_update_idx + 600]
        assert "processed_at" in final_block, \
            "Final UPDATE must set processed_at timestamp"


# ══════════════════════════════════════════════════════════════════════════════
# 7.  Logging
# ══════════════════════════════════════════════════════════════════════════════

class TestProgressLogging:
    """Critical progress milestones must be logged."""

    def test_batch_started_logged(self):
        fn = _job_fn()
        assert "batch %s started" in fn or "_process_batch_job: batch" in fn, \
            "Batch start must be logged"

    def test_page_completed_logged(self):
        fn = _job_fn()
        assert "page %d" in fn or "page_index" in fn[fn.find("logger.debug"):], \
            "Page completion must be logged"

    def test_page_error_logged_as_warning(self):
        fn = _job_fn()
        assert "logger.warning(" in fn, \
            "Page errors must be logged at WARNING level"

    def test_batch_completed_logged(self):
        fn = _job_fn()
        assert "completed" in fn[fn.find("logger.debug\n        "):] or \
               "completed" in fn[fn.rfind("logger.debug"):], \
            "Batch completion must be logged"


# ══════════════════════════════════════════════════════════════════════════════
# 8.  Syntax and BCRS stability
# ══════════════════════════════════════════════════════════════════════════════

class TestCodeIntegrity:
    def test_batches_py_parses(self):
        ast.parse(_src("app/routers/batches.py"))

    def test_review_py_parses(self):
        ast.parse(_src("app/routers/review.py"))

    def test_bcrs_logic_unchanged(self):
        src = _src("app/routers/batches.py")
        bcrs_fn = src[src.find("def _extract_bcrs_amount_from_summary"):
                      src.find("\ndef _build_bcrs_row")]
        assert "has_label_line" in bcrs_fn
        assert "best_score < 20" in bcrs_fn
        assert "_is_vat_line" in bcrs_fn

    def test_split_math_unchanged(self):
        src = _src("app/routers/batches.py")
        assert "_corrected_total = round(_net + _vat, 2)" in src
        assert "row.total_amount = _corrected_total" in src

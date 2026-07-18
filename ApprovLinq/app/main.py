import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.db.schema_version import CURRENT_ALEMBIC_REVISION, SchemaVersionError, assert_database_schema_current
from app.db.session import engine
from app.routers import analytics, auth, admin, admin_export_templates, batches, health, learning, tenant

logger = logging.getLogger(__name__)
SCHEMA_VERSION_STATUS = None
try:
    SCHEMA_VERSION_STATUS = assert_database_schema_current(engine)
    if SCHEMA_VERSION_STATUS.checked:
        logger.info("Database schema verified at Alembic revision %s", CURRENT_ALEMBIC_REVISION)
    else:
        logger.info(
            "Database schema check skipped for %s engine: %s",
            SCHEMA_VERSION_STATUS.dialect,
            SCHEMA_VERSION_STATUS.reason,
        )
except SchemaVersionError:
    logger.exception("Database schema is not ready for this build")
    raise

app = FastAPI(title=settings.app_name)
base_dir = Path(__file__).resolve().parent
static_dir = base_dir / "static"
_version_file = base_dir.parent / "VERSION"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
async def recover_stuck_batches() -> None:
    from app.db.session import SessionLocal
    from app.services.scan_jobs import release_stale_jobs, release_stale_pages
    db = SessionLocal()
    try:
        released = release_stale_jobs(db)
        released_pages = release_stale_pages(db)
        if released or released_pages:
            logger.info(
                "Released %d stale durable scan job lease(s) and %d page lease(s) on startup",
                released,
                released_pages,
            )
    except Exception as exc:
        logger.warning("Failed to recover durable scan jobs on startup: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@app.on_event("startup")
async def run_file_retention_cleanup() -> None:
    """Delete uploaded PDFs and exported XLSXs older than the configured
    retention window (default 5 days).  Runs once at startup so the server
    self-cleans on every deploy/restart without needing a cron job.

    Safety rules:
    - Only files strictly older than file_retention_days are removed.
    - Whole batch-upload folders are removed only when ALL files inside them
      are past the retention window (avoids breaking active review sessions).
    - Export files are removed individually by mtime.
    - DB records are not touched — rows remain for analytics/audit history.
    """
    import time
    import shutil
    from pathlib import Path as _Path

    retention_seconds = settings.file_retention_days * 86_400
    now = time.time()
    removed_files = removed_folders = 0

    # ── Uploaded batch folders ────────────────────────────────────────────────
    try:
        upload_root = _Path(settings.upload_dir).resolve()
        if upload_root.exists():
            for batch_folder in upload_root.iterdir():
                if not batch_folder.is_dir():
                    continue
                pdf_files = [f for f in batch_folder.rglob("*") if f.is_file()]
                if not pdf_files:
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception:
                        pass
                    continue
                newest_mtime = max(f.stat().st_mtime for f in pdf_files)
                if (now - newest_mtime) > retention_seconds:
                    try:
                        shutil.rmtree(batch_folder, ignore_errors=True)
                        removed_folders += 1
                    except Exception as exc:
                        logger.warning("Retention: could not remove folder %s: %s", batch_folder, exc)
    except Exception as exc:
        logger.warning("Retention: upload cleanup failed: %s", exc)

    # ── Exported XLSX files ───────────────────────────────────────────────────
    try:
        export_root = _Path(settings.export_dir).resolve()
        if export_root.exists():
            for export_file in export_root.rglob("*.xlsx"):
                if not export_file.is_file():
                    continue
                try:
                    if (now - export_file.stat().st_mtime) > retention_seconds:
                        export_file.unlink(missing_ok=True)
                        removed_files += 1
                except Exception as exc:
                    logger.warning("Retention: could not remove export %s: %s", export_file, exc)
    except Exception as exc:
        logger.warning("Retention: export cleanup failed: %s", exc)

    if removed_folders or removed_files:
        logger.info(
            "Retention cleanup: removed %d batch folder(s), %d export file(s) "
            "(retention=%d days)",
            removed_folders, removed_files, settings.file_retention_days,
        )



@app.get("/version")
def get_version():
    version = _version_file.read_text().strip() if _version_file.exists() else "0.0.0"
    return JSONResponse({"version": version})


@app.get("/")
def frontend():
    path = static_dir / "login.html"
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=500, detail=f"Frontend file not found: {path}")


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(batches.router)
# >>> REVIEW_PACK router_register
from app.routers import review as _review_router
app.include_router(_review_router.router)
# <<< REVIEW_PACK router_register
app.include_router(admin.router)
app.include_router(admin_export_templates.router)
app.include_router(tenant.router)
app.include_router(analytics.router)
app.include_router(learning.router)

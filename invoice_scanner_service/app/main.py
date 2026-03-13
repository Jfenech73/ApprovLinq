from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import admin, batches, health

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)

base_dir = Path(__file__).resolve().parent
static_dir = base_dir / "static"

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def frontend():
    candidates = [
        base_dir / "index.html",
        static_dir / "index.html",
    ]

    for path in candidates:
        if path.exists():
            return FileResponse(path)

    raise HTTPException(
        status_code=500,
        detail=f"Frontend file not found. Checked: {[str(p) for p in candidates]}",
    )


app.include_router(health.router)
app.include_router(batches.router)
app.include_router(admin.router)
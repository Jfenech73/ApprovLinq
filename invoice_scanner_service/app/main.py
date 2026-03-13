from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import models
from app.db.session import engine
from app.routers import admin, batches, health

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)

base_dir = Path(__file__).parent
static_dir = base_dir / "static"

static_dir.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def frontend():
    return FileResponse(base_dir / "index.html")


app.include_router(health.router)
app.include_router(batches.router)
app.include_router(admin.router)
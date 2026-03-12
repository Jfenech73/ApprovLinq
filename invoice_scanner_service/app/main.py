from fastapi import FastAPI
from app.config import settings
from app.db.session import engine
from app.db import models
from app.routers import health, batches, admin

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)

app.include_router(health.router)
app.include_router(batches.router)
app.include_router(admin.router)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings


db_url = settings.database_url.strip()
connect_args = {}
engine_kwargs = {"future": True}

if db_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_recycle"] = 180

engine = create_engine(
    db_url,
    connect_args=connect_args,
    **engine_kwargs,
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

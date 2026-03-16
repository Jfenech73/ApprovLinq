from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

db_url = settings.database_url.strip()

if db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

if db_url.startswith("postgresql+psycopg://") and "sslmode=" not in db_url:
    sep = "&" if "?" in db_url else "?"
    db_url = f"{db_url}{sep}sslmode=require"

url = make_url(db_url)
engine_kwargs = {"future": True}

if url.get_backend_name().startswith("postgresql"):
    # Managed pooler endpoints are safest with fresh connections and without
    # psycopg prepared statements.
    engine_kwargs["poolclass"] = NullPool
    engine_kwargs["connect_args"] = {
        "connect_timeout": 10,
        "prepare_threshold": None,
    }
else:
    engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
    })

engine = create_engine(db_url, **engine_kwargs)
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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from app.config import settings


db_url = settings.database_url.strip()

if db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

if db_url.startswith("postgresql+psycopg://") and "sslmode=" not in db_url:
    sep = "&" if "?" in db_url else "?"
    db_url = f"{db_url}{sep}sslmode=require"

engine_kwargs = {
    "future": True,
}

if db_url.startswith("postgresql+"):
    # Managed Postgres poolers (such as Neon pooler endpoints) are generally more
    # reliable when SQLAlchemy does not keep its own long-lived connection pool.
    # Create fresh connections on demand and rely on the provider-side pooler.
    engine_kwargs.update({
        "poolclass": NullPool,
        "connect_args": {
            "connect_timeout": 10,
        },
    })
else:
    engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
    })

engine = create_engine(db_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

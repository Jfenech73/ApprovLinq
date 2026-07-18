from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


CURRENT_ALEMBIC_REVISION = "20260718_0014"

KNOWN_ALEMBIC_REVISIONS = frozenset({
    "20260411_0001",
    "20260513_0001",
    "20260513_0002",
    "20260516_0003",
    "20260516_0004",
    "20260707_0005",
    "20260707_0006",
    "20260710_0007",
    "20260710_0008",
    "20260710_0009",
    "20260710_0010",
    "20260712_0011",
    "20260713_0012",
    "20260713_0013",
    CURRENT_ALEMBIC_REVISION,
})


class SchemaVersionError(RuntimeError):
    """Raised when a production database is not at the expected Alembic head."""


@dataclass(frozen=True)
class SchemaVersionStatus:
    checked: bool
    dialect: str
    expected_revision: str
    current_revision: str | None = None
    reason: str | None = None


def validate_alembic_revision(
    current_revision: str | None,
    *,
    expected_revision: str = CURRENT_ALEMBIC_REVISION,
) -> None:
    if not current_revision:
        raise SchemaVersionError(
            "Database schema is not versioned; run `alembic upgrade head` before starting the app."
        )
    if current_revision not in KNOWN_ALEMBIC_REVISIONS:
        raise SchemaVersionError(
            f"Database schema revision {current_revision!r} is not recognised by this build."
        )
    if current_revision != expected_revision:
        raise SchemaVersionError(
            f"Database schema revision {current_revision!r} is behind build head "
            f"{expected_revision!r}; run `alembic upgrade head` before starting the app."
        )


def assert_database_schema_current(
    engine: Engine,
    *,
    expected_revision: str = CURRENT_ALEMBIC_REVISION,
) -> SchemaVersionStatus:
    """Verify PostgreSQL schema state without mutating it.

    SQLite is still used by isolated unit tests and local throwaway fixtures, so
    this production guard only enforces Alembic state for PostgreSQL engines.
    """

    dialect = engine.dialect.name
    if dialect != "postgresql":
        return SchemaVersionStatus(
            checked=False,
            dialect=dialect,
            expected_revision=expected_revision,
            reason="schema version enforcement is PostgreSQL-only",
        )

    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            validate_alembic_revision(None, expected_revision=expected_revision)
        current_revision = conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar()

    validate_alembic_revision(str(current_revision) if current_revision else None, expected_revision=expected_revision)
    return SchemaVersionStatus(
        checked=True,
        dialect=dialect,
        expected_revision=expected_revision,
        current_revision=str(current_revision),
    )

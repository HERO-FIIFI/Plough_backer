"""SQLite engine, exact Decimal column type, and the migration gate (README §30 steps 1–2, §51).

Schema is only ever created by Alembic (`alembic upgrade head`). Startup never creates
tables; it refuses to run against a database that is not at the migration head.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, DateTime, Engine, String, create_engine, event, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.types import TypeDecorator

from plough_backer.decimal_utils import decimal_to_str, to_decimal
from plough_backer.exceptions import MigrationStateError

# ponytail: migrations live at the repo root (README §70), so this assumes a source
# checkout / editable install — the V1 deployment model (§73). Package them if that changes.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class DecimalText(TypeDecorator[Decimal]):
    """Decimal stored as exact TEXT. SQLite NUMERIC is a binary float — unusable for INV-07."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"DecimalText requires Decimal, got {type(value).__name__}")
        return decimal_to_str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else to_decimal(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes. SQLite stores naive text, so tz is normalized here."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime refused; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _sqlite_pragmas(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    # Hand transaction control to SQLAlchemy so _begin_immediate below is what starts them.
    dbapi_conn.isolation_level = None
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")  # journal durability over write speed
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise ValueError("V1 supports SQLite only")
    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    event.listen(engine, "connect", _sqlite_pragmas)
    event.listen(engine, "begin", _begin_immediate)
    return engine


def _begin_immediate(conn: Connection) -> None:
    # ponytail: every transaction takes the SQLite write lock up front, so writers are fully
    # serialized (§26) and read-modify-write can't interleave. Fine at signal-copier volume;
    # use deferred BEGIN for read-only paths if lock waits ever show up in latency (§50).
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))  # ini interpolation
    return cfg


def assert_migrations_current(engine: Engine) -> None:
    """Raise MigrationStateError unless the DB is at the single Alembic head."""
    script = ScriptDirectory.from_config(
        alembic_config(engine.url.render_as_string(hide_password=False))
    )
    heads = set(script.get_heads())
    if len(heads) != 1:
        raise MigrationStateError(f"expected exactly one migration head, found {sorted(heads)}")
    with engine.connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
    if current != heads:
        raise MigrationStateError(
            f"database at {sorted(current) or 'no revision'}, head is {sorted(heads)}; "
            "run `alembic upgrade head`"
        )


def upgrade_migrations(database_url: str) -> None:
    """Upgrade a fresh or existing database to head. Safe to call on every startup."""
    cfg = alembic_config(database_url)
    cfg.attributes["configure_logger"] = False
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        raise MigrationStateError("automatic database migration failed") from exc

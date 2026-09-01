from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base

# SQLite busy timeout (ms): how long a connection will wait for a lock held
# by another connection before raising "database is locked", instead of
# failing immediately. Given CONCURRENCY worker threads each doing multiple
# commits per migrated item, a per-item cancellation-check db.refresh(), and
# the GUI polling /jobs/{id} every 2s per visible job, short-lived lock
# contention under real load is expected and should be waited out rather
# than surfaced as an error.
_SQLITE_BUSY_TIMEOUT_MS = 30000


def _make_engine():
    settings = get_settings()
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(settings.database_url, connect_args=connect_args)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()
    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# Columns added to the schema after a table already existed in deployed
# databases. This project deliberately uses Base.metadata.create_all()
# instead of Alembic (see README) — that call only creates missing
# TABLES, it never adds a column to a table that already exists. Without
# this, a deployed instance upgrading to a newer image would crash with
# "no such column" the first time the new column is read or written.
# List new (table, column, sql_type) tuples here as the schema grows.
_ADDED_COLUMNS = [
    ("migration_job", "error_message", "TEXT"),
]


def _apply_missing_columns(target_engine=None) -> None:
    target_engine = target_engine if target_engine is not None else engine
    inspector = inspect(target_engine)
    existing_tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if table not in existing_tables:
                continue  # a fresh install already gets it from create_all()
            existing_columns = {col["name"] for col in inspector.get_columns(table)}
            if column not in existing_columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_missing_columns(engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

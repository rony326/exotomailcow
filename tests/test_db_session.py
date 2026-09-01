from sqlalchemy import text

from app.db.session import engine


def test_sqlite_engine_uses_wal_journal_mode():
    # Regression test for finding #7 (final whole-branch review): under
    # real concurrent load (CONCURRENCY worker threads, per-item
    # cancellation-check db.refresh(), GUI polling /jobs/{id} every 2s),
    # SQLite's default rollback-journal locking risks "database is locked"
    # errors. WAL mode allows concurrent readers alongside a single writer.
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_sqlite_engine_has_busy_timeout_configured():
    with engine.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms > 0

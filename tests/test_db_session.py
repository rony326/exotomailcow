import tempfile

from sqlalchemy import create_engine, inspect, text

from app.db.session import _apply_missing_columns, engine


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


def test_apply_missing_columns_adds_error_message_to_existing_table_without_data_loss():
    # Simulates a deployed database created before migration_job.error_message
    # existed: Base.metadata.create_all() only creates missing tables, it
    # never alters one that's already there, so a real upgrade needs this
    # to run against the OLD table shape without wiping existing rows.
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        scratch_engine = create_engine(f"sqlite:///{tmp.name}")
        with scratch_engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE migration_job ("
                    "id INTEGER PRIMARY KEY, mapping_id INTEGER, status VARCHAR(20)"
                    ")"
                )
            )
            conn.execute(text("INSERT INTO migration_job (id, mapping_id, status) VALUES (1, 1, 'completed')"))

        _apply_missing_columns(scratch_engine)

        inspector = inspect(scratch_engine)
        columns = {col["name"] for col in inspector.get_columns("migration_job")}
        assert "error_message" in columns

        with scratch_engine.connect() as conn:
            row = conn.execute(text("SELECT id, status, error_message FROM migration_job WHERE id = 1")).one()
        assert row.status == "completed"
        assert row.error_message is None


def test_apply_missing_columns_is_idempotent():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        scratch_engine = create_engine(f"sqlite:///{tmp.name}")
        with scratch_engine.begin() as conn:
            conn.execute(text("CREATE TABLE migration_job (id INTEGER PRIMARY KEY)"))

        _apply_missing_columns(scratch_engine)
        _apply_missing_columns(scratch_engine)  # must not raise "duplicate column"

        inspector = inspect(scratch_engine)
        columns = [col["name"] for col in inspector.get_columns("migration_job")]
        assert columns.count("error_message") == 1

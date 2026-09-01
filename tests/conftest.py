import os
import tempfile

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings

# Create a single temp database file for all tests
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)


def pytest_configure(config):
    """Set environment variables before pytest collects tests (before module imports)."""
    os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("ADMIN_USER", "admin")
    os.environ.setdefault("ADMIN_PASSWORD", "test-password")
    os.environ.setdefault("MAILCOW_DAV_BASE_URL", "https://mail.example.org")
    os.environ.setdefault("MAILCOW_IMAP_HOST", "mail.example.org")
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_path}")


def pytest_sessionfinish(session, exitstatus):
    """Clean up the temp database file after all tests."""
    try:
        os.unlink(_db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("MAILCOW_DAV_BASE_URL", "https://mail.example.org")
    monkeypatch.setenv("MAILCOW_IMAP_HOST", "mail.example.org")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{_db_path}")
    get_settings.cache_clear()

    # Initialize database tables on first run
    from app.db.session import init_db

    init_db()
    yield
    get_settings.cache_clear()

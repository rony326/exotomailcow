import pytest
from cryptography.fernet import Fernet

from app.config import get_settings


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("MAILCOW_DAV_BASE_URL", "https://mail.example.org")
    monkeypatch.setenv("MAILCOW_IMAP_HOST", "mail.example.org")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

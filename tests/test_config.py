import pytest
from pydantic import ValidationError

from app.config import get_settings


def test_settings_loads_from_env(monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.admin_user == "admin"
    assert settings.concurrency == 4
    assert settings.mailcow_imap_port == 993


def test_settings_raises_on_missing_required_var(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    get_settings.cache_clear()

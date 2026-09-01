import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.security.crypto import decrypt, encrypt


def test_encrypt_decrypt_roundtrip():
    token = encrypt("s3cr3t-app-password")
    assert token != "s3cr3t-app-password"
    assert decrypt(token) == "s3cr3t-app-password"


def test_decrypt_with_wrong_key_fails(monkeypatch):
    token = encrypt("s3cr3t-app-password")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(InvalidToken):
        decrypt(token)
    get_settings.cache_clear()

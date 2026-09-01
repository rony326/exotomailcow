from cryptography.fernet import Fernet

from app.config import get_settings


def encrypt(plaintext: str) -> str:
    settings = get_settings()
    return Fernet(settings.encryption_key.encode()).encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    settings = get_settings()
    return Fernet(settings.encryption_key.encode()).decrypt(token.encode()).decode()

import base64
import hmac
import os

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import TenantConfig
from app.db.session import get_db

_security = HTTPBasic()
_SCRYPT_PARAMS = {"length": 32, "n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = Scrypt(salt=salt, **_SCRYPT_PARAMS).derive(password.encode())
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt_b64, _, derived_b64 = password_hash.partition("$")
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(derived_b64)
    try:
        Scrypt(salt=salt, **_SCRYPT_PARAMS).verify(password.encode(), expected)
        return True
    except Exception:
        return False


def bootstrap_admin_from_env(db: Session) -> None:
    if db.query(TenantConfig).one_or_none() is not None:
        return
    settings = get_settings()
    db.add(
        TenantConfig(
            admin_user=settings.admin_user,
            admin_password_hash=hash_password(settings.admin_password),
        )
    )
    db.commit()


def require_admin(
    credentials: HTTPBasicCredentials = Depends(_security),
    db: Session = Depends(get_db),
) -> str:
    config = db.query(TenantConfig).one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not configured")
    valid_user = hmac.compare_digest(credentials.username, config.admin_user)
    valid_password = verify_password(credentials.password, config.admin_password_hash)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

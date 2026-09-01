from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, TenantConfig
from app.security.auth import bootstrap_admin_from_env, hash_password, require_admin, verify_password


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_hash_and_verify_password_roundtrip():
    hashed = hash_password("correct-horse")
    assert verify_password("correct-horse", hashed)
    assert not verify_password("wrong-password", hashed)


def test_bootstrap_admin_from_env_creates_row_once():
    db = _session()
    bootstrap_admin_from_env(db)
    assert db.query(TenantConfig).count() == 1

    bootstrap_admin_from_env(db)
    assert db.query(TenantConfig).count() == 1


def test_require_admin_enforces_credentials():
    db = _session()
    bootstrap_admin_from_env(db)

    app = FastAPI()

    @app.get("/protected")
    def protected(user: str = Depends(require_admin)):
        return {"user": user}

    app.dependency_overrides = {}
    from app.db.session import get_db as real_get_db

    def _override_get_db():
        yield db

    app.dependency_overrides[real_get_db] = _override_get_db

    client = TestClient(app)
    assert client.get("/protected").status_code == 401
    assert client.get("/protected", auth=("admin", "wrong")).status_code == 401
    ok = client.get("/protected", auth=("admin", "test-password"))
    assert ok.status_code == 200
    assert ok.json() == {"user": "admin"}

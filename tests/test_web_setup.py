from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin
from app.web.routes import setup


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(TenantConfig(admin_user="admin", admin_password_hash="unused"))
    db.commit()

    app = FastAPI()
    app.include_router(setup.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    return app, db


def test_setup_form_renders():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.get("/setup")
    assert response.status_code == 200
    assert "Tenant ID" in response.text


def test_setup_save_encrypts_client_secret():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.post(
        "/setup",
        data={"tenant_id": "tid", "client_id": "cid", "client_secret": "s3cret"},
    )
    assert response.status_code == 200

    from app.security.crypto import decrypt

    config = db.query(TenantConfig).one()
    assert config.tenant_id == "tid"
    assert config.client_secret_encrypted != "s3cret"
    assert decrypt(config.client_secret_encrypted) == "s3cret"


def _mock_tenant_discovery(monkeypatch):
    # GraphClient's constructor builds a msal.ConfidentialClientApplication, which by
    # default performs a live network call to validate the authority/tenant. Mock it out
    # (same pattern as tests/test_graph_client.py) so these tests don't depend on network
    # access or a real Azure AD tenant.
    monkeypatch.setattr(
        "msal.authority.tenant_discovery",
        lambda *args, **kwargs: {
            "token_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
            "authorization_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/authorize",
        },
    )


def test_test_connection_reports_success(monkeypatch):
    app, db = _app_and_db()
    config = db.query(TenantConfig).one()
    from app.security.crypto import encrypt

    config.tenant_id, config.client_id, config.client_secret_encrypted = "tid", "cid", encrypt("s3cret")
    db.commit()

    _mock_tenant_discovery(monkeypatch)
    monkeypatch.setattr(
        "app.web.routes.setup.GraphClient.list_mailboxes", lambda self, search=None: iter([object()])
    )

    client = TestClient(app)
    response = client.post("/setup/test-connection")
    assert "erfolgreich" in response.text


def test_test_connection_reports_failure(monkeypatch):
    app, db = _app_and_db()
    config = db.query(TenantConfig).one()
    from app.security.crypto import encrypt

    config.tenant_id, config.client_id, config.client_secret_encrypted = "tid", "cid", encrypt("s3cret")
    db.commit()

    def _raise(self, search=None):
        raise RuntimeError("auth failed")

    _mock_tenant_discovery(monkeypatch)
    monkeypatch.setattr("app.web.routes.setup.GraphClient.list_mailboxes", _raise)

    client = TestClient(app)
    response = client.post("/setup/test-connection")
    assert "fehlgeschlagen" in response.text
    assert "auth failed" in response.text

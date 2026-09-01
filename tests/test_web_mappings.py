import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db.models import Base, MailboxMapping, TenantConfig
from app.db.session import get_db
from app.graph.models import GraphMailbox
from app.security.auth import require_admin
from app.security.crypto import encrypt
from app.web.routes import mappings


def _app_and_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)

    app = FastAPI()
    app.include_router(mappings.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: "admin"
    return app, db


def test_add_mapping_encrypts_password_and_lists_it():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.post(
        "/mappings",
        data={"exo_upn": "user@church.org", "mailcow_address": "user@mailcow.local", "app_password": "pw123"},
    )
    assert response.status_code == 200
    assert "user@church.org" in response.text

    mapping = db.query(MailboxMapping).one()
    assert mapping.app_password_encrypted != "pw123"


def test_csv_import_creates_multiple_mappings():
    app, db = _app_and_db()
    client = TestClient(app)
    csv_content = "exo_upn,mailcow_address,app_password\na@x.org,a@mailcow.local,pw1\nb@x.org,b@mailcow.local,pw2\n"
    response = client.post(
        "/mappings/csv-import",
        files={"file": ("mappings.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 2


def test_csv_import_missing_required_column_returns_error_without_importing():
    app, db = _app_and_db()
    client = TestClient(app)
    # no "app_password" column
    csv_content = "exo_upn,mailcow_address\na@x.org,a@mailcow.local\n"
    response = client.post(
        "/mappings/csv-import",
        files={"file": ("mappings.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert response.status_code == 200
    assert "app_password" in response.text
    assert db.query(MailboxMapping).count() == 0


def test_csv_import_skips_malformed_row_but_imports_good_row():
    app, db = _app_and_db()
    client = TestClient(app)
    csv_content = (
        "exo_upn,mailcow_address,app_password\n"
        "good@x.org,good@mailcow.local,pw1\n"
        "bad@x.org,bad@mailcow.local\n"  # missing app_password value
    )
    response = client.post(
        "/mappings/csv-import",
        files={"file": ("mappings.csv", io.BytesIO(csv_content.encode()), "text/csv")},
    )
    assert response.status_code == 200

    mappings = db.query(MailboxMapping).all()
    assert len(mappings) == 1
    assert mappings[0].exo_upn == "good@x.org"

    # the bad row is row 3 in the file (header is row 1, good row is row 2)
    assert "3" in response.text


def test_csv_import_non_utf8_file_returns_error_not_500():
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.post(
        "/mappings/csv-import",
        files={"file": ("mappings.csv", io.BytesIO(b"exo_upn,mailcow_address,app_password\n\xff\xfe\x00bad"), "text/csv")},
    )
    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 0


def test_delete_mapping_removes_row():
    app, db = _app_and_db()
    db.add(MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc"))
    db.commit()
    mapping_id = db.query(MailboxMapping).one().id

    client = TestClient(app)
    response = client.request("DELETE", f"/mappings/{mapping_id}")

    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 0


def _mock_tenant_discovery(monkeypatch):
    # Same pattern as tests/test_web_setup.py: avoid GraphClient's constructor
    # making a live tenant-discovery network call.
    monkeypatch.setattr(
        "msal.authority.tenant_discovery",
        lambda *args, **kwargs: {
            "token_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/token",
            "authorization_endpoint": "https://login.microsoftonline.com/tid/oauth2/v2.0/authorize",
        },
    )


def test_list_mappings_renders_resync_and_purge_buttons_for_synced_mapping():
    # Regression test for finding #2 (final whole-branch review): resync and
    # the purge-secrets kill-switch had routes but no GUI entry point.
    app, db = _app_and_db()
    from datetime import datetime, timezone

    db.add(
        MailboxMapping(
            exo_upn="synced@church.org", mailcow_address="synced@mailcow.local", app_password_encrypted="enc",
            last_synced_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
    )
    db.commit()

    client = TestClient(app)
    response = client.get("/mappings")

    assert response.status_code == 200
    text = response.text
    mapping = db.query(MailboxMapping).one()
    assert f'hx-post="/mappings/{mapping.id}/resync"' in text
    assert 'hx-post="/mappings/resync-all"' in text
    assert 'hx-post="/admin/purge-secrets"' in text


def test_list_mappings_renders_graph_mailbox_dropdown(monkeypatch):
    # Regression test for finding #8 (final whole-branch review): spec §10.2
    # asks for a Graph-backed dropdown for exo_upn instead of free text.
    app, db = _app_and_db()
    db.add(
        TenantConfig(
            tenant_id="tid", client_id="cid", client_secret_encrypted=encrypt("secret"),
            admin_user="a", admin_password_hash="h",
        )
    )
    db.commit()

    _mock_tenant_discovery(monkeypatch)
    monkeypatch.setattr(
        "app.web.routes.mappings.GraphClient.list_mailboxes",
        lambda self, search=None: iter(
            [
                GraphMailbox(id="1", user_principal_name="alice@church.org", display_name="Alice", mail="alice@church.org"),
                GraphMailbox(id="2", user_principal_name="obrien@church.org", display_name="O'Brien", mail="obrien@church.org"),
            ]
        ),
    )

    client = TestClient(app)
    response = client.get("/mappings")

    assert response.status_code == 200
    assert "<select" in response.text
    assert "alice@church.org" in response.text
    assert "O&#39;Brien" in response.text or "O'Brien" in response.text


def test_list_mappings_falls_back_to_free_text_when_graph_unavailable(monkeypatch):
    # Regression test for finding #8: TenantConfig configured but Graph call
    # fails -- the page must still render (free-text fallback), not 500.
    app, db = _app_and_db()
    db.add(
        TenantConfig(
            tenant_id="tid", client_id="cid", client_secret_encrypted=encrypt("secret"),
            admin_user="a", admin_password_hash="h",
        )
    )
    db.commit()

    _mock_tenant_discovery(monkeypatch)

    def _raise(self, search=None):
        raise RuntimeError("Graph unreachable")

    monkeypatch.setattr("app.web.routes.mappings.GraphClient.list_mailboxes", _raise)

    client = TestClient(app)
    response = client.get("/mappings")

    assert response.status_code == 200
    assert '<input type="text" name="exo_upn"' in response.text
    assert '<select name="exo_upn"' not in response.text


def test_list_mappings_uses_free_text_when_tenant_config_not_set_up():
    # No TenantConfig row at all -- must not 500.
    app, db = _app_and_db()
    client = TestClient(app)
    response = client.get("/mappings")
    assert response.status_code == 200
    assert 'name="exo_upn"' in response.text


def test_list_mappings_filters_by_search_query():
    app, db = _app_and_db()
    db.add_all(
        [
            MailboxMapping(exo_upn="alice@church.org", mailcow_address="alice@mailcow.local", app_password_encrypted="e"),
            MailboxMapping(exo_upn="bob@church.org", mailcow_address="bob@mailcow.local", app_password_encrypted="e"),
        ]
    )
    db.commit()

    client = TestClient(app)
    response = client.get("/mappings", params={"q": "alice"})

    assert "alice@church.org" in response.text
    assert "bob@church.org" not in response.text

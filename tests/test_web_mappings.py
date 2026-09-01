import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db.models import Base, MailboxMapping
from app.db.session import get_db
from app.security.auth import require_admin
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


def test_delete_mapping_removes_row():
    app, db = _app_and_db()
    db.add(MailboxMapping(exo_upn="a@b", mailcow_address="a@c", app_password_encrypted="enc"))
    db.commit()
    mapping_id = db.query(MailboxMapping).one().id

    client = TestClient(app)
    response = client.request("DELETE", f"/mappings/{mapping_id}")

    assert response.status_code == 200
    assert db.query(MailboxMapping).count() == 0


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

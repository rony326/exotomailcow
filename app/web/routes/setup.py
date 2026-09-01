from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import TenantConfig
from app.db.session import get_db
from app.graph.client import GraphClient
from app.security.auth import require_admin
from app.security.crypto import decrypt, encrypt

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one()
    return templates.TemplateResponse(request, "setup.html", {"config": config, "test_result": None})


@router.post("/setup", response_class=HTMLResponse)
def setup_save(
    request: Request,
    tenant_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    config = db.query(TenantConfig).one()
    config.tenant_id = tenant_id
    config.client_id = client_id
    config.client_secret_encrypted = encrypt(client_secret)
    db.commit()
    return templates.TemplateResponse(request, "setup.html", {"config": config, "test_result": "saved"})


@router.post("/setup/test-connection", response_class=HTMLResponse)
def test_connection(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one()
    result = "error"
    error_detail = None
    if config.tenant_id and config.client_id and config.client_secret_encrypted:
        try:
            client = GraphClient(config.tenant_id, config.client_id, decrypt(config.client_secret_encrypted))
            next(iter(client.list_mailboxes()), None)
            result = "ok"
        except Exception as exc:
            error_detail = str(exc)
    else:
        error_detail = "Bitte zuerst Tenant ID, Client ID und Client Secret speichern."
    return templates.TemplateResponse(
        request, "_setup_test_result.html", {"result": result, "error_detail": error_detail}
    )

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import MailboxMapping, TenantConfig
from app.db.session import get_db
from app.security.auth import require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.post("/admin/purge-secrets", response_class=HTMLResponse)
def purge_secrets(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    config = db.query(TenantConfig).one_or_none()
    if config is not None:
        config.client_id = None
        config.client_secret_encrypted = None
    for mapping in db.query(MailboxMapping).all():
        mapping.app_password_encrypted = ""
    db.commit()
    return templates.TemplateResponse(request, "_purge_result.html", {})

import csv
import io

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.models import MailboxMapping
from app.db.session import get_db
from app.security.auth import require_admin
from app.security.crypto import encrypt

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/mappings", response_class=HTMLResponse)
def list_mappings(request: Request, q: str = "", db: Session = Depends(get_db), _: str = Depends(require_admin)):
    query = db.query(MailboxMapping)
    if q:
        query = query.filter(MailboxMapping.exo_upn.contains(q))
    mappings = query.order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(request, "mappings.html", {"mappings": mappings, "q": q})


@router.post("/mappings", response_class=HTMLResponse)
def add_mapping(
    request: Request,
    exo_upn: str = Form(...),
    mailcow_address: str = Form(...),
    app_password: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
):
    db.add(
        MailboxMapping(
            exo_upn=exo_upn, mailcow_address=mailcow_address, app_password_encrypted=encrypt(app_password)
        )
    )
    db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(request, "_mappings_table.html", {"mappings": mappings})


@router.post("/mappings/csv-import", response_class=HTMLResponse)
async def import_mappings_csv(
    request: Request, file: UploadFile, db: Session = Depends(get_db), _: str = Depends(require_admin)
):
    content = (await file.read()).decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    count = 0
    for row in reader:
        db.add(
            MailboxMapping(
                exo_upn=row["exo_upn"],
                mailcow_address=row["mailcow_address"],
                app_password_encrypted=encrypt(row["app_password"]),
            )
        )
        count += 1
    db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(
        request, "_mappings_table.html", {"mappings": mappings, "imported_count": count}
    )


@router.delete("/mappings/{mapping_id}", response_class=HTMLResponse)
def delete_mapping(
    request: Request, mapping_id: int, db: Session = Depends(get_db), _: str = Depends(require_admin)
):
    mapping = db.get(MailboxMapping, mapping_id)
    if mapping is not None:
        db.delete(mapping)
        db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(request, "_mappings_table.html", {"mappings": mappings})

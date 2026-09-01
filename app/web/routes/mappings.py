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

REQUIRED_CSV_COLUMNS = ["exo_upn", "mailcow_address", "app_password"]


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
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
        return templates.TemplateResponse(
            request,
            "_mappings_table.html",
            {
                "mappings": mappings,
                "csv_error": (
                    "Die Datei konnte nicht gelesen werden: Sie muss UTF-8-kodiert sein. "
                    "Bitte die CSV-Datei in UTF-8 speichern und erneut hochladen."
                ),
            },
        )

    reader = csv.DictReader(io.StringIO(content))
    missing_columns = [col for col in REQUIRED_CSV_COLUMNS if col not in (reader.fieldnames or [])]
    if missing_columns:
        mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
        return templates.TemplateResponse(
            request,
            "_mappings_table.html",
            {
                "mappings": mappings,
                "csv_error": (
                    "Fehlende Spalte(n) in der CSV-Datei: "
                    + ", ".join(missing_columns)
                    + ". Erwartet werden die Spalten: exo_upn, mailcow_address, app_password."
                ),
            },
        )

    count = 0
    skipped_rows = []
    # row_number is 1-based counting the header as row 1, matching what a spreadsheet
    # program shows, so an admin can find the offending line in their CSV directly.
    for row_number, row in enumerate(reader, start=2):
        try:
            exo_upn = row["exo_upn"]
            mailcow_address = row["mailcow_address"]
            app_password = row["app_password"]
            if not exo_upn or not mailcow_address or not app_password:
                raise ValueError("Pflichtfeld fehlt oder ist leer (exo_upn, mailcow_address, app_password)")
            db.add(
                MailboxMapping(
                    exo_upn=exo_upn,
                    mailcow_address=mailcow_address,
                    app_password_encrypted=encrypt(app_password),
                )
            )
            count += 1
        except Exception as exc:
            skipped_rows.append((row_number, str(exc)))

    db.commit()
    mappings = db.query(MailboxMapping).order_by(MailboxMapping.created_at).all()
    return templates.TemplateResponse(
        request,
        "_mappings_table.html",
        {"mappings": mappings, "imported_count": count, "skipped_rows": skipped_rows},
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

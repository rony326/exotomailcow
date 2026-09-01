# Exchange Online → Mailcow Migrationstool — Design

Status: approved (Architektur), Stand 2026-09-01

## 1. Kontext & Ziel

Migration einer Kirchgemeinde-Organisation von Microsoft 365 (Exchange
Online) auf eine selbstgehostete Mailcow-Instanz (Dovecot IMAP, Postfix,
SOGo für CalDAV/CardDAV). Ein Docker-betreibbares Tool mit Web-GUI
migriert pro Postfach Mail (inkl. Ordnerstruktur), Kalender und Kontakte
von EXO nach Mailcow, mit Fortschrittsanzeige, Fehlerbehandlung,
Resume-Fähigkeit und Abschlussbericht.

Erwarteter Umfang: 20+ Postfächer, mittleres bis größeres Datenvolumen →
Job-Runner mit begrenzter Parallelität statt rein sequenziell.

## 2. Nicht-Ziele

- Keine Delegated-User-OAuth-Flows — nur App-Only/Admin-Consent
  (Client-Credentials-Flow via MSAL).
- Kein automatisches Anlegen der Ziel-Postfächer in Mailcow — müssen
  vorher existieren.
- Keine Echtzeit-Sync/Coexistence-Phase — Einmal-Migrationslauf, aber
  wiederholbar/resumable für Nachläufe.
- Kein EAS-Adapter (Begründung siehe Abschnitt 8).

## 3. Architektur-Überblick

Ein Docker-Container, ein FastAPI-Prozess. Kein separater Worker-Service:
Migrationen laufen als Background-Jobs im selben Prozess über einen
`ThreadPoolExecutor`, weil die Ziel-Protokolle (IMAP, CalDAV, CardDAV)
über synchrone Python-Libraries angesprochen werden (`imapclient`,
`caldav`) und das Vermischen von async Graph-Calls mit sync IMAP/CalDAV
innerhalb einer Job-Funktion unnötige Komplexität erzeugen würde. Jede
Mailbox-Migration läuft komplett synchron in einem Worker-Thread.

Persistenz: SQLite (via SQLAlchemy), sowohl für Konfiguration/Mappings
als auch als einzige Quelle der Wahrheit für Job- und Item-Status —
das GUI fragt Fortschritt direkt aus der DB per HTMX-Polling ab, es gibt
keinen In-Memory-Zustand, der einen Absturz nicht überleben würde.

### Projektstruktur

```
exotomailcow/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── README.md
├── app/
│   ├── main.py                    # FastAPI-App, Startup-Hook: resume_incomplete_jobs()
│   ├── config.py                  # Settings (pydantic-settings), Fernet-Key aus ENV
│   ├── db/
│   │   ├── models.py              # SQLAlchemy-Modelle
│   │   ├── session.py
│   │   └── migrations/            # Alembic
│   ├── security/
│   │   ├── crypto.py              # Fernet encrypt/decrypt für Secrets
│   │   └── auth.py                # Basic-Auth-Dependency fürs GUI
│   ├── graph/
│   │   ├── client.py              # GraphClient: MSAL Client-Credentials + httpx (synchron)
│   │   ├── models.py              # GraphMailbox, GraphFolder, GraphMessage, GraphEvent, GraphContact
│   │   └── retry.py               # 429/Retry-After + 5xx Backoff (Decorator, wiederverwendet in Importern)
│   ├── importers/
│   │   ├── base.py                # Protocols: MailImporter, CalendarImporter, ContactImporter
│   │   ├── imap_importer.py       # IMAP APPEND, Ordner-Mapping, Delimiter-Erkennung
│   │   ├── caldav_importer.py     # SOGo CalDAV PUT
│   │   └── carddav_importer.py    # SOGo CardDAV PUT
│   ├── conversion/
│   │   ├── ics.py                 # Graph-Event → iCalendar (icalendar-Lib)
│   │   └── vcard.py               # Graph-Contact → vCard (vobject-Lib)
│   ├── jobs/
│   │   ├── runner.py              # MigrationJobRunner: eine Mailbox komplett abarbeiten
│   │   └── scheduler.py           # ThreadPoolExecutor-Pool, Concurrency-Limit, Resume beim Start
│   ├── web/
│   │   ├── routes/                # setup.py, mappings.py, jobs.py
│   │   ├── templates/             # Jinja2 + HTMX-Partials
│   │   └── static/
│   └── logging_config.py          # JSON-Logs, Secret-Redaction-Filter, Rotation
└── tests/
    ├── unit/
    └── integration/                # gegen ein einzelnes Test-Postfach, manuell markiert
```

## 4. Datenmodell (SQLite via SQLAlchemy)

```
tenant_config (Singleton-Zeile)
  id, tenant_id, client_id, client_secret_encrypted,
  admin_user, admin_password_hash, created_at

mailbox_mapping
  id, exo_upn, mailcow_address, app_password_encrypted, created_at

migration_job
  id, mapping_id → mailbox_mapping,
  status            (pending/running/completed/failed/cancelled),
  migrate_mail bool, migrate_calendar bool, migrate_contacts bool,
  mail_since_date   (nullable, Zeitraum-Filter),
  dry_run bool,
  created_at, started_at, finished_at

migration_item        -- Idempotenz-Kern
  id, mapping_id → mailbox_mapping,   -- an mapping_id, NICHT job_id gebunden,
                                       -- damit Nachlauf-Jobs für dieselbe Mapping-Zeile
                                       -- weiterhin gegen alles bereits Importierte dedupen
  category          (mail/calendar/contacts),
  external_id       (Graph message/event/contact-ID),
  content_hash      (Fallback, falls Graph-ID sich ändert),
  status            (done/skipped/failed),
  target_ref        (IMAP UID / CalDAV href / CardDAV href),
  error_message,
  updated_at
  UNIQUE(mapping_id, category, external_id)

mail_folder_map
  id, mapping_id → mailbox_mapping,
  graph_folder_id, graph_path,        -- z.B. "Inbox/Projekte/2024"
  imap_mailbox_name,                  -- mit erkanntem Dovecot-Delimiter aufgebaut
  well_known_type nullable,           -- inbox/sentitems/deleteditems/drafts/archive
  created bool
```

## 5. Kern-Interfaces

### GraphClient (`app/graph/client.py`)

Synchron (bewusst, siehe Abschnitt 3), MSAL `ConfidentialClientApplication`
für Client-Credentials-Token, `httpx.Client` für REST-Calls.

```python
class GraphClient:
    def list_mailboxes(self, search: str | None = None) -> Iterator[GraphMailbox]: ...
    def list_mail_folders(self, user_id: str) -> list[GraphFolder]:        # rekursiv aufgelöster Baum
    def list_messages(self, user_id: str, folder_id: str,
                       since: datetime | None = None) -> Iterator[GraphMessageRef]: ...
    def get_message_raw(self, user_id: str, message_id: str) -> bytes:     # $value MIME
    def list_calendars(self, user_id: str) -> list[GraphCalendar]: ...
    def list_events(self, user_id: str, calendar_id: str) -> Iterator[GraphEvent]: ...
    def list_contacts(self, user_id: str) -> Iterator[GraphContact]: ...
```

### Importer-Interfaces (`app/importers/base.py`, als `Protocol`)

```python
class MailImporter(Protocol):
    def connect(self, target: MailcowTarget) -> str: ...                  # gibt erkannten Delimiter zurück
    def ensure_folder(self, imap_path: str) -> None: ...
    def append_message(self, imap_path: str, raw_mime: bytes,
                        flags: list[str], internal_date: datetime) -> str: ...  # UID
    def close(self) -> None: ...

class CalendarImporter(Protocol):
    def put_event(self, target: MailcowTarget, uid: str, ics_data: bytes) -> str: ...  # href

class ContactImporter(Protocol):
    def put_contact(self, target: MailcowTarget, uid: str, vcard_data: bytes) -> str: ...
```

Duplikaterkennung für Kalender/Kontakte läuft — konsistent mit Mail —
ausschließlich über `migration_item` (dort als `external_id` die
Graph-Event-/Kontakt-ID, `content_hash` optional als Fallback bei
UID-Kollisionen). Es gibt bewusst **keinen** zusätzlichen Remote-Check
gegen SOGo (`event_exists`/`contact_exists`) — das wäre ein zweiter,
redundanter Idempotenz-Mechanismus mit unnötigen Roundtrips.

### JobRunner & Scheduler (`app/jobs/`)

```python
class MigrationJobRunner:
    def run(self, job_id: int) -> None:
        # lädt Job + Mapping, entschlüsselt Secrets (nur im Thread-lokalen Scope),
        # für jede aktivierte Kategorie: migrate_mail() / migrate_calendar() / migrate_contacts()
        # pro Item: migration_item.status == 'done'? → skip; sonst migrieren + Zeile schreiben
        # Fehler pro Item: Retry mit Backoff, nach N Versuchen 'failed', Job läuft weiter
```

`scheduler.py`: `ThreadPoolExecutor(max_workers=CONCURRENCY)` (ENV,
Default 4). Beim FastAPI-Startup werden alle `migration_job` mit
`status = running` auf `pending` zurückgesetzt und der Queue erneut
zugeführt (Absturz-Resume). Bereits `done`-markierte `migration_item`-
Zeilen werden dabei übersprungen.

## 6. Ordner-Mapping-Logik (Mail)

1. `GET /users/{id}/mailFolders` gepaged, rekursiv aufgelöst (Graph
   expandiert `childFolders` nicht beliebig tief — bei
   `childFolderCount > 0` wird gezielt nachgeladen).
2. Well-known-Folder über Graph's `wellKnownName` erkannt (`inbox`,
   `sentitems`, `deleteditems`, `drafts`, `archive`) → feste IMAP-Namen
   (`INBOX`, `Sent`, `Trash`, `Drafts`, `Archive`).
3. Alle anderen Ordner: Pfad aus Displaynamen zusammengesetzt (z.B.
   `Projekte/2024`).
4. Delimiter wird **nicht** hart angenommen: bei `connect()` via IMAP
   `CAPABILITY` + `LIST "" ""` das tatsächliche Trennzeichen ermittelt,
   Pfad damit zusammengesetzt.
5. `mail_folder_map` cached Graph-ID → IMAP-Name pro Mapping; IMAP
   `CREATE` nur beim ersten Mal.

## 7. Fehlerbehandlung & Resilienz

- Einheitlicher Retry/Backoff-Decorator (`app/graph/retry.py`),
  wiederverwendet in allen Importern: exponentielles Backoff mit
  Jitter; Graph-429 nutzt den `Retry-After`-Header exakt.
- IMAP/CalDAV/CardDAV-Verbindungsfehler: Retry mit Backoff, nach N
  Versuchen `migration_item.status = failed` + `error_message`, Job
  läuft mit dem nächsten Item weiter (kein Job-Abbruch wegen einem
  fehlerhaften Item).
- Resume nach Absturz/Neustart: siehe Abschnitt 5 (Scheduler).

## 8. Sicherheit

- **Secrets-Verschlüsselung:** `cryptography.fernet.Fernet`, Key
  ausschließlich aus ENV-Var `ENCRYPTION_KEY` (32 Byte, base64) — nie in
  der DB oder im Image. Client-Secret und App-Passwörter werden vor dem
  Schreiben verschlüsselt, erst beim tatsächlichen API/IMAP/CalDAV-Call
  entschlüsselt.
- **GUI-Login:** HTTP Basic Auth. `ADMIN_USER`/`ADMIN_PASSWORD` aus ENV
  werden beim First-Run gehasht in `tenant_config` übernommen, ENV
  danach ignoriert.
- **Logging:** JSON-strukturiert, Rotation via `RotatingFileHandler`.
  Ein Logging-Filter redigiert aktiv bekannte Secret-Felder
  (`client_secret`, `app_password`, `Authorization`-Header), bevor ein
  Log-Record geschrieben wird.
- **Kill-Switch:** Route/Button "Alle Zugangsdaten löschen" — löscht
  `tenant_config`-Secret-Spalten und alle `app_password_encrypted`-Werte
  in `mailbox_mapping`. Migrations-Historie (`migration_item`,
  `migration_job`, ohne Secrets) bleibt für Abschlussberichte erhalten.

## 9. EAS-Pfad — Begründung gegen Implementierung

Kein Exchange-ActiveSync-Adapter. Gründe: (a) die verfügbaren
Python-EAS-Bibliotheken sind unausgereift/unmaintained, (b) Mailcow/SOGo
bieten IMAP, CalDAV und CardDAV nativ und robust an — es gibt zielseitig
gar keinen EAS-Server, gegen den importiert werden könnte. Graph deckt
den Quell-Zugriff bereits vollständig ab. Wird im README dokumentiert.

## 10. GUI-Workflow

1. **Setup:** Azure-AD-Credentials eingeben (Tenant-ID, Client-ID,
   Client-Secret), "Verbindung testen" (ruft `GET /users` mit 1 Result
   ab, zeigt Erfolg/Fehler).
2. **Mapping:** Tabelle Quelle (EXO-UPN, Dropdown aus `GET /users`
   mit Suchfeld) → Ziel (Mailcow-Adresse, Freitext) → App-Passwort
   (maskiert) → "Hinzufügen". CSV-Bulk-Import
   (`exo_upn,mailcow_address,app_password`).
3. **Job-Optionen:** pro Mapping-Zeile oder als Batch: Mail/Kalender/
   Kontakte togglebar, Zeitraum-Filter für Mail, Dry-Run-Modus (zählt
   nur, schreibt nichts).
4. **Ausführung:** Start (einzeln oder Batch), Live-Fortschritt pro
   Postfach/Kategorie (Ordner X/Y, Nachricht N/M) via HTMX-Polling
   gegen `migration_job`/`migration_item`, Abbrechen-Button (setzt
   `status = cancelled`, Worker-Thread prüft das Flag zwischen Items).
5. **Abschlussbericht:** pro Postfach Anzahl migriert/übersprungen/
   fehlgeschlagen, Fehlerliste mit Klartext-Grund, Download als CSV/JSON.

## 11. Deployment

Ein Dockerfile (Python 3.11-slim), `docker-compose.yml` mit einem
Service, Volume für die SQLite-Datei. ENV-Variablen: `ENCRYPTION_KEY`,
`ADMIN_USER`, `ADMIN_PASSWORD`, `CONCURRENCY` (Default 4), plus
Mailcow/SOGo-Basis-URL als Default-Vorschlag im Mapping-Formular.

README dokumentiert: Azure-AD-App-Registration mit den Application
Permissions `Mail.ReadWrite`, `Calendars.Read`, `Contacts.Read`,
`MailboxSettings.Read`, `User.Read.All`, Admin-Consent-Schritt,
Mailcow-Voraussetzungen (App-Passwort-Feature aktivieren, SOGo-URL-
Schema), vollständige ENV-Variablen-Liste, EAS-Begründung (Abschnitt 9).

## 12. Testing

- Unit-Tests: Ordner-Mapping-Logik, Delimiter-Erkennung, Retry/Backoff,
  Idempotenz-Logik (`migration_item`-Dedup), Fernet-Verschlüsselung.
- Integrationstests gegen ein einzelnes Test-Postfach (EXO-Quelle +
  Mailcow-Ziel), manuell markiert (kein CI-Zwang, da echte Zugangsdaten
  nötig): Mail-Rundlauf inkl. verschachtelter Ordner, Kalender-Rundlauf
  inkl. Serientermin, Kontakte-Rundlauf, Resume nach simuliertem Absturz.

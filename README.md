# exotomailcow

Migrationstool: Exchange Online (Microsoft Graph, App-Only) → Mailcow
(Dovecot IMAP + SOGo CalDAV/CardDAV). Migriert pro Postfach Mail
(inkl. Ordnerstruktur), Kalender und Kontakte, mit Web-GUI,
Fortschrittsanzeige, Resume-Fähigkeit und Post-Cutover-Resync.

Architektur- und Designentscheidungen: siehe
`docs/superpowers/specs/2026-09-01-exo-to-mailcow-migration-tool-design.md`.

## 1. Azure AD App-Registration

1. Azure Portal → **Azure Active Directory → App registrations → New registration**.
   Name frei wählbar (z.B. `exotomailcow-migration`), kein Redirect-URI nötig.
2. Nach der Erstellung: **Certificates & secrets → New client secret**,
   Wert sofort kopieren (wird nur einmal angezeigt) → das ist `CLIENT_SECRET`
   fürs Setup im Tool.
3. **API permissions → Add a permission → Microsoft Graph → Application permissions**,
   folgende hinzufügen:
   - `Mail.ReadWrite`
   - `Calendars.Read`
   - `Contacts.Read`
   - `User.Read.All`
4. **Grant admin consent for `<tenant>`** klicken (erfordert Global-Admin-
   oder Privileged-Role-Admin-Rechte) — ohne diesen Schritt schlagen alle
   Graph-Aufrufe mit `403 Forbidden` fehl, egal wie die Permissions
   konfiguriert sind.
5. Tenant-ID (**Overview → Directory (tenant) ID**) und Application
   (Client) ID notieren.

Diese drei Werte (Tenant-ID, Client-ID, Client-Secret) werden im Tool
unter **Setup** eingetragen, dort Fernet-verschlüsselt in der DB
abgelegt und nie im Klartext geloggt.

## 2. Mailcow-Voraussetzungen

- **App-Passwörter aktivieren:** Mailcow Admin-UI → *Configuration →
  Access → App Passwords* (bzw. pro Mailbox im User-Bereich unter
  *App Passwords*) — für jedes Ziel-Postfach ein App-Passwort erzeugen,
  das im Mapping als `app_password` eingetragen wird. Normale
  Mailbox-Passwörter funktionieren zwar auch, App-Passwörter sind aber
  die empfohlene, widerrufbare Variante für automatisierte Zugriffe.
- **IMAP** ist bei Mailcow standardmäßig auf Port 993 (SSL) aktiv —
  `MAILCOW_IMAP_HOST` auf den Mailcow-Hostnamen setzen.
- **SOGo-URL:** Mailcow bindet SOGo unter `https://<mailcow-host>/SOGo/`
  ein; `MAILCOW_DAV_BASE_URL` ist die Basis-URL ohne `/SOGo`-Suffix
  (z.B. `https://mail.example.org`), das Tool hängt
  `/SOGo/dav/<adresse>/Calendar/personal/` bzw.
  `/Contacts/personal/` selbst an (`personal` ist SOGos
  Standard-Collection für den privaten Kalender/das private
  Adressbuch eines Users).
  **Achtung:** Dieses URL-Schema konnte während der Entwicklung nicht
  gegen eine echte Mailcow/SOGo-Instanz verifiziert werden (kein
  Testserver verfügbar). Das genaue Verhalten bzgl. Collection-Namen
  kann je nach SOGo-Version/Konfiguration abweichen — vor einer
  echten Migration unbedingt mit einem einzelnen Test-Postfach gegen
  die tatsächliche Zielinstanz verifizieren (z.B. per manuellem
  `curl -X PUT` gegen die o.g. URL, oder Beobachtung der Requests im
  Dry-Run/ersten echten Lauf), bevor ein produktiver Massen-Import
  gestartet wird.
- **Ziel-Postfächer müssen vorher existieren** — das Tool legt keine
  Mailcow-Mailboxen an (bewusstes Nicht-Ziel, siehe Spec §2).

## 3. Environment-Variablen

| Variable | Pflicht | Default | Beschreibung |
|---|---|---|---|
| `ENCRYPTION_KEY` | ja | – | Fernet-Key (32 Byte, base64) für Secrets-Verschlüsselung. Erzeugen mit `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Verlust = alle gespeicherten Secrets unbrauchbar.** |
| `ADMIN_USER` | ja | – | Benutzername fürs GUI-Login (Basic Auth), nur beim allerersten Start wirksam. |
| `ADMIN_PASSWORD` | ja | – | Passwort fürs GUI-Login, nur beim allerersten Start wirksam (danach in der DB gehasht gespeichert; zum Ändern die `tenant_config`-Zeile löschen und neu starten). |
| `MAILCOW_DAV_BASE_URL` | ja | – | Basis-URL der Mailcow-Instanz für SOGo CalDAV/CardDAV, z.B. `https://mail.example.org`. |
| `MAILCOW_IMAP_HOST` | ja | – | Hostname des Mailcow-Dovecot-IMAP-Servers. |
| `MAILCOW_IMAP_PORT` | nein | `993` | IMAP-SSL-Port. |
| `CONCURRENCY` | nein | `4` | Anzahl gleichzeitig laufender Postfach-Migrationen. |
| `DATABASE_URL` | nein | `sqlite:///./data/exotomailcow.db` | SQLite-Pfad (im Container `sqlite:////app/data/exotomailcow.db`, viertes `/` beachten). |
| `LOG_DIR` | nein | `./data/logs` | Verzeichnis für rotierende JSON-Logs. |
| `LOG_LEVEL` | nein | `INFO` | Log-Level. |

## 4. Betrieb

```bash
cp .env.example .env   # Werte oben ausfüllen
docker compose build
docker compose up -d
```

GUI unter `http://<host>:8000`, Login mit `ADMIN_USER`/`ADMIN_PASSWORD`.
Workflow: **Setup** (Azure-AD-Zugangsdaten, Verbindung testen) →
**Postfächer** (Mappings anlegen oder CSV importieren) → pro Mapping
oder als Batch eine Migration starten → Fortschritt live verfolgen →
Abschlussbericht (CSV/JSON) herunterladen.

**Nach der DNS-Umstellung** (MX/Autodiscover → Mailcow): pro Postfach
oder für alle auf einmal **Resync** anstoßen, um Mail/Termine/Kontakte
nachzuziehen, die zwischen Erstmigration und Umstellung in EXO
hinzukamen oder sich änderten (siehe Spec §6a).

**Kill-Switch:** unter *Admin* → "Alle Zugangsdaten löschen" entfernt
Client-Secret und alle App-Passwörter aus der DB, sobald die Migration
abgeschlossen ist. Migrations-Historie und Berichte bleiben erhalten.

## 5. Warum kein Exchange-ActiveSync-Adapter?

IMAP/CalDAV/CardDAV sind die einzigen Ziel-Adapter. Gründe: die
verfügbaren Python-EAS-Bibliotheken sind unausgereift/unmaintained, und
Mailcow/SOGo bieten IMAP, CalDAV und CardDAV nativ und robust an — es
gibt zielseitig ohnehin keinen EAS-Server, gegen den importiert werden
könnte. Graph deckt den Quell-Zugriff bereits vollständig ab.

## 6. Entwicklung & Tests

```bash
pip install -e ".[dev]"
pytest                          # vollständige Unit-/Komponenten-Suite
```

### Kein Integrationstest gegen echte EXO/Mailcow-Server — aktueller Stand

Es existiert **noch kein** `tests/integration`-Verzeichnis und **keine**
`integration`-Marker-Registrierung in `pyproject.toml`. Dieses Tool
wurde bisher ausschließlich gegen Unit-/Komponenten-Tests mit
gefälschten (Fake/Mock) Graph-, IMAP- und CalDAV/CardDAV-Clients
verifiziert — **nicht** gegen eine echte Exchange-Online- oder
Mailcow-Instanz. Das ist eine bewusste Scope-Grenze (siehe
`docs/superpowers/plans/2026-09-01-exo-to-mailcow-migration-tool.md`,
Abschnitt "Post-Plan Note"): ein solcher End-to-End-Test braucht echte
EXO- und Mailcow-Testzugangsdaten, die während der Entwicklung nicht
zur Verfügung standen, und ein Fake/Mock-Ersatz dafür wäre nicht
aussagekräftig.

Sobald echte Testzugangsdaten vorhanden sind, sollte ein Mensch
`tests/integration/test_single_mailbox_roundtrip.py` von Hand nach
Spec §12 schreiben (Wiederverwendung der bestehenden `GraphClient`,
`ImapMailImporter`, `CalDavCalendarImporter`, `CardDavContactImporter`
und `MigrationJobRunner` — keine neuen Schnittstellen nötig) und dabei
mindestens folgende Szenarien abdecken:

- Mail-Rundlauf inkl. verschachtelter Ordner
- Kalender-Rundlauf inkl. Serientermin
- Kontakte-Rundlauf
- Resume nach simuliertem Absturz
- ein vollständiger Resync-Rundlauf

**Vor einer echten Migration eines Produktiv-Postfachs wird dringend
empfohlen, das Tool zunächst manuell gegen ein einzelnes Test-Postfach
zu verifizieren** (Mapping anlegen, Dry-Run, dann echten Lauf mit
kleinem Postfach), auch ohne den oben beschriebenen automatisierten
Integrationstest — insbesondere wegen des unter Punkt 2 genannten,
nicht gegen einen echten SOGo-Server verifizierten DAV-URL-Schemas.

from datetime import datetime, timezone

import httpx

from app.graph.client import GraphClient


def _client(handler, monkeypatch) -> GraphClient:
    def mock_tenant_discovery(*args, **kwargs):
        return {
            "token_endpoint": "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token",
            "authorization_endpoint": "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/authorize",
        }

    monkeypatch.setattr("msal.authority.tenant_discovery", mock_tenant_discovery)
    monkeypatch.setattr(
        "app.graph.client.msal.ConfidentialClientApplication.acquire_token_for_client",
        lambda self, scopes: {"access_token": "fake-token"},
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    return GraphClient("tenant-id", "client-id", "client-secret", http_client=http_client)


def test_list_mailboxes_single_page(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        assert request.url.path.endswith("/users")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "1", "userPrincipalName": "a@church.org", "displayName": "A", "mail": "a@church.org"},
                ]
            },
        )

    client = _client(handler, monkeypatch)
    mailboxes = list(client.list_mailboxes())

    assert len(mailboxes) == 1
    assert mailboxes[0].user_principal_name == "a@church.org"


def test_list_mailboxes_follows_next_link(monkeypatch):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "1", "userPrincipalName": "a@church.org", "displayName": "A", "mail": "a@church.org"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=abc",
                },
            )
        return httpx.Response(
            200,
            json={"value": [{"id": "2", "userPrincipalName": "b@church.org", "displayName": "B", "mail": "b@church.org"}]},
        )

    client = _client(handler, monkeypatch)
    mailboxes = list(client.list_mailboxes())

    assert calls["count"] == 2
    assert [m.id for m in mailboxes] == ["1", "2"]


def test_list_mailboxes_escapes_search_quotes(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filter"] = request.url.params["$filter"]
        return httpx.Response(200, json={"value": []})

    client = _client(handler, monkeypatch)
    list(client.list_mailboxes(search="O'Brien"))

    assert "O''Brien" in captured["filter"]


def _well_known_folder_handler(user_id: str, ids_by_name: dict) -> callable:
    """Serves GET /users/{user_id}/mailFolders/{name} for the 5 well-known
    names, 404-ing for any name not present in ids_by_name (mirroring a
    mailbox that doesn't have e.g. an Archive folder)."""

    def handle(request: httpx.Request) -> httpx.Response | None:
        prefix = f"/v1.0/users/{user_id}/mailFolders/"
        path = request.url.path
        if not path.startswith(prefix):
            return None
        name = path[len(prefix):]
        if name not in GraphClient._WELL_KNOWN_FOLDER_NAMES:
            return None
        if name in ids_by_name:
            return httpx.Response(200, json={"id": ids_by_name[name]})
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "not found"}})

    return handle


def test_list_mail_folders_resolves_nested_children(monkeypatch):
    well_known = _well_known_folder_handler("user-1", {"inbox": "inbox"})

    def handler(request: httpx.Request) -> httpx.Response:
        well_known_response = well_known(request)
        if well_known_response is not None:
            return well_known_response
        path = request.url.path
        if path.endswith("/mailFolders"):
            assert "wellKnownName" not in request.url.params.get("$select", "")
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "inbox", "displayName": "Inbox", "parentFolderId": None, "childFolderCount": 0},
                        {"id": "root-custom", "displayName": "Projekte", "parentFolderId": None, "childFolderCount": 1},
                    ]
                },
            )
        if path.endswith("root-custom/childFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "child-1", "displayName": "2024", "parentFolderId": "root-custom", "childFolderCount": 0},
                    ]
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = _client(handler, monkeypatch)
    folders = client.list_mail_folders("user-1")

    assert {f.id for f in folders} == {"inbox", "root-custom", "child-1"}
    child = next(f for f in folders if f.id == "child-1")
    assert child.parent_id == "root-custom"
    inbox = next(f for f in folders if f.id == "inbox")
    assert inbox.well_known_name == "inbox"
    custom = next(f for f in folders if f.id == "root-custom")
    assert custom.well_known_name is None


def test_resolve_well_known_folder_ids_maps_ids_and_skips_missing_ones(monkeypatch):
    # Mailbox has everything except Archive (a real, common case -- Archive
    # is opt-in per mailbox).
    well_known = _well_known_folder_handler(
        "user-1",
        {
            "inbox": "graph-inbox-id",
            "sentitems": "graph-sent-id",
            "deleteditems": "graph-trash-id",
            "drafts": "graph-drafts-id",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = well_known(request)
        if response is not None:
            return response
        raise AssertionError(f"unexpected path {request.url.path}")

    client = _client(handler, monkeypatch)
    ids = client._resolve_well_known_folder_ids("user-1")

    assert ids == {
        "graph-inbox-id": "inbox",
        "graph-sent-id": "sentitems",
        "graph-trash-id": "deleteditems",
        "graph-drafts-id": "drafts",
    }


def test_list_messages_maps_flags_and_applies_since_filter(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["filter"] = request.url.params.get("$filter")
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": "m1", "receivedDateTime": "2026-01-01T10:00:00Z", "isRead": True,
                     "flag": {"flagStatus": "flagged"}},
                    {"id": "m2", "receivedDateTime": "2026-01-02T10:00:00Z", "isRead": False, "flag": {}},
                ]
            },
        )

    client = _client(handler, monkeypatch)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    messages = list(client.list_messages("user-1", "inbox", since=since))

    assert captured["filter"] == "receivedDateTime ge 2026-01-01T00:00:00Z"
    assert messages[0].is_read is True
    assert messages[0].is_flagged is True
    assert messages[1].is_flagged is False


def test_get_message_raw_returns_bytes(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages/m1/$value")
        return httpx.Response(200, content=b"From: a@b\r\nSubject: hi\r\n\r\nbody")

    client = _client(handler, monkeypatch)
    raw = client.get_message_raw("user-1", "m1")

    assert raw.startswith(b"From: a@b")


def test_list_events_requests_utc_and_applies_modified_since_filter(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["prefer"] = request.headers.get("Prefer")
        captured["filter"] = request.url.params.get("$filter")
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "evt1",
                        "iCalUId": "uid-1",
                        "lastModifiedDateTime": "2026-02-01T09:00:00Z",
                        "subject": "Sitzung",
                        "start": {"dateTime": "2026-02-05T10:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-02-05T11:00:00.0000000", "timeZone": "UTC"},
                        "isAllDay": False,
                        "location": {"displayName": "Saal"},
                        "body": {"content": "Agenda"},
                        "organizer": {"emailAddress": {"address": "leiter@church.org"}},
                        "attendees": [{"emailAddress": {"address": "mitglied@church.org"}}],
                        "recurrence": None,
                    }
                ]
            },
        )

    client = _client(handler, monkeypatch)
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = list(client.list_events("user-1", "cal-1", modified_since=since))

    assert captured["prefer"] == 'outlook.timezone="UTC"'
    assert captured["filter"] == "lastModifiedDateTime ge 2026-01-01T00:00:00Z"
    assert events[0].ics_uid == "uid-1"
    assert events[0].organizer_email == "leiter@church.org"
    assert events[0].attendees == ["mitglied@church.org"]
    assert events[0].start.tzinfo is not None


def test_list_events_falls_back_to_graph_id_when_ical_uid_is_null(monkeypatch):
    # Regression test: a real tenant returned iCalUId: null for an old
    # event, which crashed the migration_item NOT NULL constraint and
    # produced a CalDAV URL ending in "/None.ics" (event.ics_uid was used
    # unguarded as both the dedup key and the CalDAV filename).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "evt-no-uid",
                        "iCalUId": None,
                        "lastModifiedDateTime": "2020-07-18T06:48:18.237166Z",
                        "subject": "Altes Ereignis",
                        "start": {"dateTime": "2020-07-18T10:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2020-07-18T11:00:00.0000000", "timeZone": "UTC"},
                        "isAllDay": False,
                        "location": None,
                        "body": {"content": ""},
                        "organizer": {"emailAddress": {"address": "leiter@church.org"}},
                        "attendees": [],
                        "recurrence": None,
                    }
                ]
            },
        )

    client = _client(handler, monkeypatch)
    events = list(client.list_events("user-1", "cal-1"))

    assert events[0].ics_uid == "evt-no-uid"


def test_list_contacts_maps_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "c1",
                        "lastModifiedDateTime": "2026-02-01T09:00:00Z",
                        "displayName": "Maria Muster",
                        "emailAddresses": [{"address": "maria@example.org"}],
                        "businessPhones": ["+41 44 000 00 00"],
                        "mobilePhone": "+41 79 000 00 00",
                        "companyName": "Kirchgemeinde",
                    }
                ]
            },
        )

    client = _client(handler, monkeypatch)
    contacts = list(client.list_contacts("user-1"))

    assert contacts[0].display_name == "Maria Muster"
    assert contacts[0].email_addresses == ["maria@example.org"]
    assert contacts[0].mobile_phone == "+41 79 000 00 00"

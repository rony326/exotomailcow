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


def test_list_mail_folders_resolves_nested_children(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/mailFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "inbox", "displayName": "Inbox", "parentFolderId": None,
                         "wellKnownName": "inbox", "childFolderCount": 0},
                        {"id": "root-custom", "displayName": "Projekte", "parentFolderId": None,
                         "wellKnownName": None, "childFolderCount": 1},
                    ]
                },
            )
        if path.endswith("root-custom/childFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "child-1", "displayName": "2024", "parentFolderId": "root-custom",
                         "wellKnownName": None, "childFolderCount": 0},
                    ]
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = _client(handler, monkeypatch)
    folders = client.list_mail_folders("user-1")

    assert {f.id for f in folders} == {"inbox", "root-custom", "child-1"}
    child = next(f for f in folders if f.id == "child-1")
    assert child.parent_id == "root-custom"


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

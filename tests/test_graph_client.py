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

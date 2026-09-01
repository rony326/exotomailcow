import httpx
import pytest

from app.graph.retry import MAX_RETRIES, graph_request


def test_retries_on_429_and_honors_retry_after(monkeypatch):
    calls = {"count": 0}
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    response = graph_request(client, "GET", "/users")

    assert response.status_code == 200
    assert calls["count"] == 2
    assert sleeps == [2.0]


def test_retries_on_5xx_then_succeeds(monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("random.uniform", lambda a, b: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    response = graph_request(client, "GET", "/users")

    assert response.status_code == 200
    assert calls["count"] == 3


def test_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    with pytest.raises(httpx.HTTPStatusError):
        graph_request(client, "GET", "/users")


def test_client_error_message_includes_graph_error_code_and_message():
    # Regression test: httpx's default raise_for_status() message
    # ("Client error '400 Bad Request' for url '...'") tells an operator
    # nothing about *why* -- Graph's actual diagnostic is in the JSON body.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "MailboxNotEnabledForRESTAPI",
                    "message": "The mailbox is either inactive, soft-deleted, or is hosted on-premise.",
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        graph_request(client, "GET", "/users/nobody@example.org/mailFolders")

    assert "MailboxNotEnabledForRESTAPI" in str(excinfo.value)
    assert "hosted on-premise" in str(excinfo.value)


def test_client_error_without_json_body_still_raises_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"not json")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://graph.microsoft.com/v1.0")
    with pytest.raises(httpx.HTTPStatusError):
        graph_request(client, "GET", "/users")

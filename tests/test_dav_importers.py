import base64

import httpx
import pytest

from app.importers.base import MailcowTarget
from app.importers.caldav_importer import CalDavCalendarImporter
from app.importers.carddav_importer import CardDavContactImporter

_TARGET = MailcowTarget(
    address="user@mailcow.local",
    app_password="app-pass",
    imap_host="mail.example.org",
    dav_base_url="https://mail.example.org",
)


def test_put_event_sends_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["Content-Type"]
        captured["auth"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(201)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CalDavCalendarImporter(http_client=http_client)
    href = importer.put_event(_TARGET, "uid-1", b"BEGIN:VCALENDAR...")

    assert captured["method"] == "PUT"
    assert captured["url"] == "https://mail.example.org/SOGo/dav/user@mailcow.local/Calendar/uid-1.ics"
    assert captured["content_type"] == "text/calendar; charset=utf-8"
    expected_auth = "Basic " + base64.b64encode(b"user@mailcow.local:app-pass").decode()
    assert captured["auth"] == expected_auth
    assert href == "https://mail.example.org/SOGo/dav/user@mailcow.local/Calendar/uid-1.ics"


def test_put_event_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CalDavCalendarImporter(http_client=http_client)

    with pytest.raises(httpx.HTTPStatusError):
        importer.put_event(_TARGET, "uid-1", b"BEGIN:VCALENDAR...")


def test_put_contact_sends_correct_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers["Content-Type"]
        return httpx.Response(201)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = CardDavContactImporter(http_client=http_client)
    href = importer.put_contact(_TARGET, "c1", b"BEGIN:VCARD...")

    assert captured["url"] == "https://mail.example.org/SOGo/dav/user@mailcow.local/Contacts/c1.vcf"
    assert captured["content_type"] == "text/vcard; charset=utf-8"
    assert href == captured["url"]

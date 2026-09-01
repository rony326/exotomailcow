from datetime import datetime
from unittest.mock import MagicMock

from app.importers.base import MailcowTarget
from app.importers.imap_importer import ImapMailImporter

_TARGET = MailcowTarget(
    address="user@mailcow.local",
    app_password="app-pass",
    imap_host="mail.example.org",
    dav_base_url="https://mail.example.org",
)


def test_connect_returns_detected_delimiter(monkeypatch):
    mock_client = MagicMock()
    mock_client.list_folders.return_value = [((b"\\HasNoChildren",), b".", "INBOX")]
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    delimiter = importer.connect(_TARGET)

    assert delimiter == "."
    mock_client.login.assert_called_once_with("user@mailcow.local", "app-pass")


def test_ensure_folder_creates_when_missing(monkeypatch):
    mock_client = MagicMock()
    mock_client.folder_exists.return_value = False
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.ensure_folder("Projekte.2024")

    mock_client.create_folder.assert_called_once_with("Projekte.2024")


def test_ensure_folder_skips_when_present(monkeypatch):
    mock_client = MagicMock()
    mock_client.folder_exists.return_value = True
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.ensure_folder("INBOX")

    mock_client.create_folder.assert_not_called()


def test_append_message_parses_appenduid(monkeypatch):
    mock_client = MagicMock()
    mock_client.append.return_value = b"* OK [APPENDUID 38505 42] Success"
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    uid = importer.append_message("INBOX", b"raw-mime", ["\\Seen"], datetime(2026, 1, 1))

    assert uid == "42"
    mock_client.append.assert_called_once_with("INBOX", b"raw-mime", flags=["\\Seen"], msg_time=datetime(2026, 1, 1))


def test_append_message_raises_without_appenduid(monkeypatch):
    mock_client = MagicMock()
    mock_client.append.return_value = b"* OK Success"
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)

    import pytest

    with pytest.raises(RuntimeError):
        importer.append_message("INBOX", b"raw-mime", [], datetime(2026, 1, 1))


def test_close_logs_out(monkeypatch):
    mock_client = MagicMock()
    mock_client.list_folders.return_value = []
    monkeypatch.setattr("app.importers.imap_importer.IMAPClient", MagicMock(return_value=mock_client))

    importer = ImapMailImporter()
    importer.connect(_TARGET)
    importer.close()

    mock_client.logout.assert_called_once()

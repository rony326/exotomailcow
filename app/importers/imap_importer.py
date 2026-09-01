import re
from datetime import datetime

from imapclient import IMAPClient

from app.importers.base import MailcowTarget

_APPENDUID_RE = re.compile(rb"APPENDUID \d+ (\d+)")


class ImapMailImporter:
    def __init__(self) -> None:
        self._client: IMAPClient | None = None

    def connect(self, target: MailcowTarget) -> str:
        self._client = IMAPClient(target.imap_host, port=target.imap_port, ssl=True)
        self._client.login(target.address, target.app_password)
        folders = self._client.list_folders(directory="", pattern="*")
        if folders:
            delimiter = folders[0][1]
            return delimiter.decode() if isinstance(delimiter, bytes) else delimiter
        return "."

    def ensure_folder(self, imap_path: str) -> None:
        assert self._client is not None
        if not self._client.folder_exists(imap_path):
            self._client.create_folder(imap_path)

    def append_message(
        self, imap_path: str, raw_mime: bytes, flags: list[str], internal_date: datetime
    ) -> str:
        assert self._client is not None
        response = self._client.append(imap_path, raw_mime, flags=flags, msg_time=internal_date)
        match = _APPENDUID_RE.search(response)
        if not match:
            raise RuntimeError(f"IMAP server did not return APPENDUID for folder {imap_path!r}")
        return match.group(1).decode()

    def close(self) -> None:
        if self._client is not None:
            self._client.logout()
            self._client = None

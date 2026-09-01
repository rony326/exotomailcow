from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class MailcowTarget:
    address: str
    app_password: str
    imap_host: str
    dav_base_url: str
    imap_port: int = 993


class MailImporter(Protocol):
    def connect(self, target: MailcowTarget) -> str: ...
    def ensure_folder(self, imap_path: str) -> None: ...
    def append_message(
        self, imap_path: str, raw_mime: bytes, flags: list[str], internal_date: datetime
    ) -> str: ...
    def close(self) -> None: ...


class CalendarImporter(Protocol):
    def put_event(self, target: MailcowTarget, uid: str, ics_data: bytes) -> str: ...


class ContactImporter(Protocol):
    def put_contact(self, target: MailcowTarget, uid: str, vcard_data: bytes) -> str: ...

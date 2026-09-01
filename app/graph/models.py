from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def parse_graph_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class GraphMailbox:
    id: str
    user_principal_name: str
    display_name: str
    mail: str | None


@dataclass
class GraphFolder:
    id: str
    display_name: str
    parent_id: str | None
    well_known_name: str | None
    child_folder_count: int


@dataclass
class GraphMessageRef:
    id: str
    received_date_time: datetime
    is_read: bool
    is_flagged: bool


@dataclass
class GraphCalendar:
    id: str
    name: str


@dataclass
class GraphEvent:
    id: str
    ics_uid: str
    last_modified_date_time: datetime
    subject: str
    start: datetime
    end: datetime
    is_all_day: bool
    location: str | None
    body_html: str | None
    organizer_email: str | None
    attendees: list[str] = field(default_factory=list)
    recurrence: dict | None = None


@dataclass
class GraphContact:
    id: str
    last_modified_date_time: datetime
    display_name: str
    email_addresses: list[str] = field(default_factory=list)
    business_phones: list[str] = field(default_factory=list)
    mobile_phone: str | None = None
    company_name: str | None = None

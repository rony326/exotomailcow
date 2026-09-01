import html as html_lib
import re
from datetime import datetime, timezone

from icalendar import Calendar, Event, vRecur

from app.graph.models import GraphEvent

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

_DAY_MAP = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
    "friday": "FR", "saturday": "SA", "sunday": "SU",
}
_INDEX_MAP = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}


def graph_recurrence_to_rrule(recurrence: dict) -> str:
    pattern = recurrence["pattern"]
    rng = recurrence["range"]
    interval = pattern.get("interval", 1)
    parts: list[str] = []

    pattern_type = pattern["type"]
    if pattern_type == "daily":
        parts.append("FREQ=DAILY")
    elif pattern_type == "weekly":
        parts.append("FREQ=WEEKLY")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        if days:
            parts.append(f"BYDAY={days}")
    elif pattern_type == "absoluteMonthly":
        parts.append("FREQ=MONTHLY")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")
    elif pattern_type == "relativeMonthly":
        parts.append("FREQ=MONTHLY")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        parts.append(f"BYDAY={days}")
        parts.append(f"BYSETPOS={_INDEX_MAP[pattern['index']]}")
    elif pattern_type == "absoluteYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"BYMONTH={pattern['month']}")
        parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")
    elif pattern_type == "relativeYearly":
        parts.append("FREQ=YEARLY")
        parts.append(f"BYMONTH={pattern['month']}")
        days = ",".join(_DAY_MAP[d] for d in pattern.get("daysOfWeek", []))
        parts.append(f"BYDAY={days}")
        parts.append(f"BYSETPOS={_INDEX_MAP[pattern['index']]}")
    else:
        raise ValueError(f"Unsupported recurrence pattern type: {pattern_type}")

    parts.append(f"INTERVAL={interval}")

    wkst_day = pattern.get("firstDayOfWeek", "sunday")
    parts.append(f"WKST={_DAY_MAP[wkst_day]}")

    range_type = rng["type"]
    if range_type == "endDate":
        end_date = rng["endDate"].replace("-", "")
        parts.append(f"UNTIL={end_date}T235959Z")
    elif range_type == "numbered":
        parts.append(f"COUNT={rng['numberOfOccurrences']}")

    return ";".join(parts)


def _html_to_text(html: str) -> str:
    stripped = _TAG_RE.sub("", html)
    unescaped = html_lib.unescape(stripped)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


def graph_event_to_ics(event: GraphEvent) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//exotomailcow//migration//DE")
    cal.add("version", "2.0")

    vevent = Event()
    vevent.add("uid", event.ics_uid)
    vevent.add("summary", event.subject)
    vevent.add("dtstart", event.start)
    vevent.add("dtend", event.end)
    vevent.add("last-modified", event.last_modified_date_time)
    vevent.add("dtstamp", datetime.now(timezone.utc))
    if event.location:
        vevent.add("location", event.location)
    if event.body_html:
        vevent.add("description", _html_to_text(event.body_html))
    if event.organizer_email:
        vevent.add("organizer", f"mailto:{event.organizer_email}")
    for attendee in event.attendees:
        vevent.add("attendee", f"mailto:{attendee}")
    if event.recurrence:
        vevent.add("rrule", vRecur.from_ical(graph_recurrence_to_rrule(event.recurrence)))

    cal.add_component(vevent)
    return cal.to_ical()

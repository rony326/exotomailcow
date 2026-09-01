from datetime import datetime, timezone

from app.conversion.ics import graph_event_to_ics, graph_recurrence_to_rrule
from app.graph.models import GraphEvent


def test_weekly_recurrence_with_end_date():
    recurrence = {
        "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "wednesday", "friday"]},
        "range": {"type": "endDate", "endDate": "2026-12-31"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=WEEKLY" in rrule
    assert "BYDAY=MO,WE,FR" in rrule
    assert "UNTIL=20261231T235959Z" in rrule


def test_absolute_monthly_recurrence_with_count():
    recurrence = {
        "pattern": {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 15},
        "range": {"type": "numbered", "numberOfOccurrences": 5},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=MONTHLY" in rrule
    assert "BYMONTHDAY=15" in rrule
    assert "COUNT=5" in rrule


def test_relative_monthly_last_friday_no_end():
    recurrence = {
        "pattern": {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["friday"], "index": "last"},
        "range": {"type": "noEnd"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "FREQ=MONTHLY" in rrule
    assert "BYDAY=FR" in rrule
    assert "BYSETPOS=-1" in rrule
    assert "UNTIL" not in rrule
    assert "COUNT" not in rrule


def test_weekly_recurrence_default_wkst_is_sunday():
    recurrence = {
        "pattern": {"type": "weekly", "interval": 2, "daysOfWeek": ["sunday", "wednesday"]},
        "range": {"type": "noEnd"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "WKST=SU" in rrule


def test_weekly_recurrence_with_explicit_monday_wkst():
    recurrence = {
        "pattern": {
            "type": "weekly",
            "interval": 2,
            "daysOfWeek": ["sunday", "wednesday"],
            "firstDayOfWeek": "monday",
        },
        "range": {"type": "noEnd"},
    }
    rrule = graph_recurrence_to_rrule(recurrence)
    assert "WKST=MO" in rrule


def test_html_body_is_converted_to_plain_text_description():
    event = _sample_event()
    event.body_html = "<p>Hallo <b>allen</b>!</p>&amp; mehr"
    ics_bytes = graph_event_to_ics(event)
    text = ics_bytes.decode("utf-8")
    description_line = next(line for line in text.splitlines() if line.startswith("DESCRIPTION"))
    assert "<" not in description_line
    assert ">" not in description_line
    assert "Hallo allen!& mehr" in description_line


def test_graph_event_to_ics_contains_dtstamp():
    ics_bytes = graph_event_to_ics(_sample_event())
    text = ics_bytes.decode("utf-8")
    assert any(line.startswith("DTSTAMP:") for line in text.splitlines())


def _sample_event(recurrence=None) -> GraphEvent:
    return GraphEvent(
        id="evt1",
        ics_uid="uid-1",
        last_modified_date_time=datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc),
        subject="Sitzung",
        start=datetime(2026, 2, 5, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 2, 5, 11, 0, tzinfo=timezone.utc),
        is_all_day=False,
        location="Saal",
        body_html="Agenda",
        organizer_email="leiter@church.org",
        attendees=["mitglied@church.org"],
        recurrence=recurrence,
    )


def test_graph_event_to_ics_contains_core_fields():
    ics_bytes = graph_event_to_ics(_sample_event())
    text = ics_bytes.decode("utf-8")
    assert "UID:uid-1" in text
    assert "SUMMARY:Sitzung" in text
    assert "ORGANIZER:mailto:leiter@church.org" in text
    assert "ATTENDEE:mailto:mitglied@church.org" in text


def test_graph_event_to_ics_includes_rrule_when_recurring():
    recurrence = {
        "pattern": {"type": "daily", "interval": 1},
        "range": {"type": "noEnd"},
    }
    ics_bytes = graph_event_to_ics(_sample_event(recurrence))
    text = ics_bytes.decode("utf-8")
    assert "RRULE" in text
    assert "FREQ=DAILY" in text

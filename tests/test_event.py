"""Unit tests: date handling, label mapping, HTML round-trip, config parsing."""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from trilium_calendar_mcp.config import parse_calendars
from trilium_calendar_mcp.errors import CalendarError
from trilium_calendar_mcp.event import (
    Event,
    parse_date_value,
    parse_datetime_value,
    parse_time_value,
    stable_uid,
)
from trilium_calendar_mcp.htmlconv import html_to_text, text_to_html

SH = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


# ── datetime parsing ──────────────────────────────────────────────────────


def test_date_only_is_all_day():
    assert parse_datetime_value("2026-07-08", SH) == (date(2026, 7, 8), None)


def test_naive_datetime_is_read_in_calendar_timezone():
    assert parse_datetime_value("2026-07-08T11:00:00", SH) == (date(2026, 7, 8), time(11, 0))


def test_utc_datetime_is_converted_to_calendar_timezone():
    assert parse_datetime_value("2026-07-08T03:00:00Z", SH) == (date(2026, 7, 8), time(11, 0))


def test_conversion_can_roll_over_the_date():
    # 20:00Z is 04:00 the next day in Shanghai.
    assert parse_datetime_value("2026-07-07T20:00:00Z", SH) == (date(2026, 7, 8), time(4, 0))


def test_explicit_timezone_interprets_naive_input():
    # 15:00 read as UTC, stored as Shanghai wall clock.
    assert parse_datetime_value("2026-07-08T15:00:00", SH, "UTC") == (date(2026, 7, 8), time(23, 0))


def test_explicit_timezone_can_shift_the_day():
    assert parse_datetime_value("2026-07-08T23:30:00", SH, "UTC") == (date(2026, 7, 9), time(7, 30))


def test_seconds_are_dropped():
    assert parse_datetime_value("2026-07-08T11:00:45", SH) == (date(2026, 7, 8), time(11, 0))


@pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-01T10:00:00"])
def test_invalid_datetime_raises(bad):
    with pytest.raises(CalendarError):
        parse_datetime_value(bad, SH)


@pytest.mark.parametrize("bad", ["25:00", "10:75", "10", "10:0"])
def test_invalid_time_raises(bad):
    with pytest.raises(CalendarError):
        parse_time_value(bad)


def test_date_value_uses_the_date_part_of_a_datetime():
    assert parse_date_value("2026-07-08T11:00:00", "start_date") == date(2026, 7, 8)


@pytest.mark.parametrize("bad", ["", "the 8th", "2026/07/08"])
def test_invalid_date_raises(bad):
    with pytest.raises(CalendarError):
        parse_date_value(bad, "start_date")


# ── labels ────────────────────────────────────────────────────────────────


def test_all_day_single_day_omits_end_date():
    ev = Event(uid="u1", title="t", start_date=date(2026, 7, 8))
    labels = ev.labels()
    assert labels["startDate"] == "2026-07-08"
    assert "endDate" not in labels
    assert "startTime" not in labels


def test_all_day_range_keeps_inclusive_end():
    ev = Event(uid="u1", title="t", start_date=date(2026, 7, 8), end_date=date(2026, 7, 28))
    labels = ev.labels()
    assert labels["endDate"] == "2026-07-28"  # inclusive: the last day, not the day after
    assert "startTime" not in labels


def test_timed_event_labels():
    ev = Event(
        uid="u1",
        title="t",
        start_date=date(2026, 7, 8),
        start_time=time(11, 0),
        end_time=time(20, 0),
    )
    labels = ev.labels()
    assert labels["startTime"] == "11:00"
    assert labels["endTime"] == "20:00"
    assert "endDate" not in labels  # same day
    assert not ev.all_day


def test_timed_event_spanning_days_keeps_end_date():
    ev = Event(
        uid="u1",
        title="t",
        start_date=date(2026, 7, 8),
        end_date=date(2026, 7, 9),
        start_time=time(23, 0),
        end_time=time(1, 0),
    )
    assert ev.labels()["endDate"] == "2026-07-09"


def test_categories_become_comma_separated_tags():
    ev = Event(uid="u1", title="t", start_date=date(2026, 7, 8), categories=["a", "b"])
    assert ev.labels()["tags"] == "a,b"


def test_validate_rejects_reversed_dates():
    ev = Event(uid="u", title="t", start_date=date(2026, 7, 8), end_date=date(2026, 7, 1))
    with pytest.raises(CalendarError):
        ev.validate()


def test_validate_rejects_empty_title():
    with pytest.raises(CalendarError):
        Event(uid="u", title="  ", start_date=date(2026, 7, 8)).validate()


def test_validate_rejects_end_time_before_start_time_on_same_day():
    ev = Event(
        uid="u",
        title="t",
        start_date=date(2026, 7, 8),
        start_time=time(15, 0),
        end_time=time(1, 0),
    )
    with pytest.raises(CalendarError):
        ev.validate()


def test_stable_uid_is_deterministic_and_calendar_scoped():
    a = stable_uid("cal1", "wuwa-3.7-banner-1")
    assert a == stable_uid("cal1", "wuwa-3.7-banner-1")  # same key -> same uid
    assert a != stable_uid("cal1", "wuwa-3.7-banner-2")
    assert a != stable_uid("cal2", "wuwa-3.7-banner-1")  # other calendar, other uid


def test_stable_uid_is_a_valid_uuid():
    import uuid

    assert uuid.UUID(stable_uid("cal", "k"))


# ── note -> event ─────────────────────────────────────────────────────────


def _note(**labels) -> dict:
    return {
        "noteId": "abc123",
        "title": "Pool opens",
        "attributes": [
            {"type": "label", "name": "label:startDate", "value": "promoted,date", "isInheritable": True},
            {"type": "label", "name": "startDate", "value": labels.get("startDate", "2026-07-08")},
            *[
                {"type": "label", "name": name, "value": value}
                for name, value in labels.items()
                if name != "startDate"
            ],
        ],
    }


def test_from_note_ignores_inherited_template_labels():
    ev = Event.from_note(_note(caldavUID="uid-1"), content="<p>hi</p>", html_to_text=html_to_text)
    assert ev.uid == "uid-1"
    assert ev.title == "Pool opens"
    assert ev.description == "hi"


def test_from_note_falls_back_to_note_id_for_uid():
    ev = Event.from_note(_note(), html_to_text=html_to_text)
    assert ev.uid == "abc123@trilium"


def test_from_note_requires_start_date():
    with pytest.raises(CalendarError):
        Event.from_note({"noteId": "x", "title": "t", "attributes": []})


def test_from_note_round_trips_a_timed_event():
    original = Event(
        uid="uid-2",
        title="Kickoff",
        start_date=date(2026, 8, 20),
        start_time=time(4, 0),
        end_time=time(5, 0),
        categories=["game"],
        location="online",
    )
    note = _note(**original.labels())
    restored = Event.from_note(note, html_to_text=html_to_text)
    assert restored.labels() == original.labels()


# ── overlap / presentation ────────────────────────────────────────────────


def test_overlap_uses_inclusive_last_day():
    ev = Event(uid="u", title="t", start_date=date(2026, 7, 8), end_date=date(2026, 7, 28))
    assert ev.overlaps(date(2026, 7, 28), date(2026, 8, 1))
    assert not ev.overlaps(date(2026, 7, 29), date(2026, 8, 1))
    assert ev.overlaps(date(2026, 6, 1), date(2026, 7, 8))


def test_to_result_all_day_reports_inclusive_end():
    ev = Event(
        uid="u",
        title="t",
        start_date=date(2026, 7, 8),
        end_date=date(2026, 7, 28),
        calendar_name="Gaming",
    )
    result = ev.to_result(SH)
    assert result["start"] == "2026-07-08"
    assert result["end"] == "2026-07-28"
    assert result["all_day"] is True
    assert result["calendar_name"] == "Gaming"


def test_to_result_timed_carries_offset():
    ev = Event(uid="u", title="t", start_date=date(2026, 7, 8), start_time=time(11, 0))
    result = ev.to_result(SH)
    assert result["start"] == "2026-07-08T11:00:00+08:00"


# ── html round-trip ───────────────────────────────────────────────────────


def test_text_html_round_trip_preserves_lines():
    text = "line one\n\nline three"
    assert html_to_text(text_to_html(text)) == text


def test_round_trip_escapes_html():
    text = "a < b & c > d"
    assert html_to_text(text_to_html(text)) == text


def test_empty_description_produces_blank_paragraph():
    assert text_to_html("") == "<p></p>"


def test_html_to_text_handles_rich_text():
    html = "<p>Hello <strong>world</strong></p><ul><li>one</li><li>two</li></ul>"
    text = html_to_text(html)
    assert "Hello **world**" in text
    assert "- one" in text


# ── config ────────────────────────────────────────────────────────────────


def test_parse_calendars_with_alias_and_default():
    specs = parse_calendars("0hq1wCTuDBjA:Gaming, kf8Xq2vLpZ1a : Personal,third")
    assert [(s.note_id, s.alias, s.default) for s in specs] == [
        ("0hq1wCTuDBjA", "Gaming", True),
        ("kf8Xq2vLpZ1a", "Personal", False),
        ("third", None, False),
    ]


def test_parse_calendars_accepts_equals_and_newlines():
    specs = parse_calendars("a=One\nb:Two")
    assert [s.alias for s in specs] == ["One", "Two"]


def test_parse_calendars_ignores_duplicates_and_blanks():
    specs = parse_calendars("a,,a:Gaming,")
    assert len(specs) == 1 and specs[0].alias == "Gaming"

"""Integration tests against a live Trilium instance (skipped without credentials)."""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from trilium_calendar_mcp.errors import CalendarError
from trilium_calendar_mcp.event import Event, new_uid
from trilium_calendar_mcp.htmlconv import html_to_text, text_to_html

pytestmark = pytest.mark.integration


def _labels(store, note_id: str) -> dict:
    note = store._note(note_id)
    return {
        a["name"]: a.get("value", "")
        for a in note["attributes"]
        if a["type"] == "label" and not a.get("isInheritable") and not a["name"].startswith("label:")
    }


def test_resolve_accepts_alias_note_id_and_title(temp_calendar):
    store, note_id = temp_calendar.store, temp_calendar.note_id
    assert store.resolve("TestCal").note_id == note_id
    assert store.resolve("testcal").note_id == note_id  # case-insensitive
    assert store.resolve(note_id).note_id == note_id
    assert store.resolve("__trilium_calendar_mcp_test__").note_id == note_id  # by note title
    assert store.resolve(None).note_id == note_id  # default calendar

    with pytest.raises(CalendarError) as err:
        store.resolve("Gaming")
    assert "TestCal" in str(err.value)  # the error names what does exist


def test_create_all_day_range_and_read_back(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    created = store.create_event(
        cal,
        Event(
            uid=uid,
            title="Pool opens",
            start_date=date(2026, 7, 8),
            end_date=date(2026, 7, 28),
            description="upper half",
            categories=["gacha", "event"],
            location="in-game",
        ),
    )
    assert created.note_id

    labels = _labels(store, created.note_id)
    assert labels["startDate"] == "2026-07-08"
    assert labels["endDate"] == "2026-07-28"  # inclusive last day
    assert labels["caldavUID"] == uid
    assert labels["tags"] == "gacha,event"
    assert labels["location"] == "in-game"
    assert "startTime" not in labels

    fetched = store.get_event(cal, uid)
    assert fetched.title == "Pool opens"
    assert fetched.all_day and fetched.last_date == date(2026, 7, 28)
    assert fetched.categories == ["gacha", "event"]
    assert fetched.description == "upper half"
    assert fetched.calendar_name == "TestCal"

    store.delete_event(cal, uid)


def test_description_survives_the_collection_template(temp_calendar):
    """The collection's inherited ~template must not win over the event body."""
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    created = store.create_event(
        cal,
        Event(uid=uid, title="Template trap", start_date=date(2026, 7, 8), description="Line A\nLine B"),
    )
    raw = store._content(created.note_id)
    assert raw.strip() == text_to_html("Line A\nLine B").strip()
    assert store.get_event(cal, uid).description == "Line A\nLine B"

    store.delete_event(cal, uid)


def test_event_without_description_does_not_keep_template_body(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    created = store.create_event(cal, Event(uid=uid, title="No body", start_date=date(2026, 7, 8)))
    assert html_to_text(store._content(created.note_id)) == ""

    store.delete_event(cal, uid)


def test_update_transitions_all_day_to_timed_and_drops_stale_labels(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    store.create_event(
        cal,
        Event(
            uid=uid,
            title="Before",
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 25),
            categories=["old"],
            description="old body",
        ),
    )
    note_id = store.get_event(cal, uid).note_id

    store.update_event(
        cal,
        uid,
        {
            "title": "After",
            "start_date": date(2026, 8, 20),
            "start_time": time(4, 0),
            "end_time": time(5, 0),
            "end_date": None,
            "categories": [],
            "description": "new body",
        },
    )

    labels = _labels(store, note_id)
    assert labels["startTime"] == "04:00"
    assert labels["endTime"] == "05:00"
    assert "endDate" not in labels  # stale label removed on transition
    assert "tags" not in labels  # cleared categories are removed, not left behind
    assert store._note(note_id)["title"] == "After"

    refreshed = store.get_event(cal, uid)
    assert not refreshed.all_day
    assert refreshed.start_time == time(4, 0)
    assert refreshed.categories == []
    assert refreshed.description == "new body"

    store.delete_event(cal, uid)


def test_upsert_is_idempotent_on_uid(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    first, created = store.upsert_event(
        cal, Event(uid=uid, title="v1", start_date=date(2026, 9, 1))
    )
    assert created is True

    second, created_again = store.upsert_event(
        cal, Event(uid=uid, title="v2", start_date=date(2026, 9, 2), categories=["a"])
    )
    assert created_again is False
    assert second.note_id == first.note_id  # same note, updated in place
    assert store._note(first.note_id)["title"] == "v2"
    assert store.get_event(cal, uid).start_date == date(2026, 9, 2)

    matching = [e for e in store.list_events(cal, limit=0, include_descriptions=False) if e.uid == uid]
    assert len(matching) == 1

    store.delete_event(cal, uid)


def test_list_filters_and_ordering(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uids = [new_uid() for _ in range(3)]
    events = [
        Event(uid=uids[0], title="Alpha launch", start_date=date(2026, 10, 1), categories=["game"]),
        Event(uid=uids[1], title="Beta patch", start_date=date(2026, 10, 15), categories=["patch"]),
        Event(uid=uids[2], title="Gamma stream", start_date=date(2026, 11, 5), location="Online"),
    ]
    for event in events:
        store.create_event(cal, event)

    window = store.list_events(cal, start=date(2026, 10, 1), end=date(2026, 10, 31))
    assert [e.uid for e in window] == [uids[0], uids[1]]  # sorted, range-bounded

    assert [e.uid for e in store.list_events(cal, title_contains="patch")] == [uids[1]]
    assert [e.uid for e in store.list_events(cal, categories="patch,other")] == [uids[1]]
    assert [e.uid for e in store.list_events(cal, location_contains="online")] == [uids[2]]
    assert [e.uid for e in store.list_events(cal, limit=1)] == [uids[0]]
    # date-scoped so leftovers from a failed run cannot reorder the assertion
    assert [
        e.uid
        for e in store.list_events(cal, start=date(2026, 10, 1), end=date(2026, 10, 31), limit=1)
    ] == [uids[0]]

    for uid in uids:
        store.delete_event(cal, uid)


def test_update_without_description_returns_the_stored_body(temp_calendar):
    """Regression: an update that does not touch the body must not report ''.

    The returned event used to be built from an empty placeholder, so callers
    saw description: "" while the note still held the original text.
    """
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    store.create_event(
        cal,
        Event(uid=uid, title="keep my body", start_date=date(2026, 11, 10),
              description="line one\nline two"),
    )
    updated = store.update_event(cal, uid, {"title": "renamed only"})

    assert updated.title == "renamed only"
    assert updated.description == "line one\nline two"  # not ""
    assert updated.to_result(ZoneInfo("Asia/Shanghai"))["description"] == "line one\nline two"
    # and the note still holds it
    assert store.get_event(cal, uid).description == "line one\nline two"

    store.delete_event(cal, uid)


def test_cross_day_timed_event_round_trip(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    created = store.create_event(
        cal,
        Event(uid=uid, title="overnight", start_date=date(2026, 11, 12), start_time=time(23, 0),
              end_date=date(2026, 11, 13), end_time=time(1, 0)),
    )
    labels = _labels(store, created.note_id)
    assert labels["startDate"] == "2026-11-12" and labels["startTime"] == "23:00"
    assert labels["endDate"] == "2026-11-13" and labels["endTime"] == "01:00"

    result = store.get_event(cal, uid).to_result(ZoneInfo("Asia/Shanghai"))
    assert result["end"] == "2026-11-13T01:00:00+08:00"
    assert result["end_date"] == "2026-11-13"

    store.delete_event(cal, uid)


def test_same_day_short_event_reports_its_end_date(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()

    created = store.create_event(
        cal,
        Event(uid=uid, title="30 min", start_date=date(2026, 11, 11),
              start_time=time(15, 0), end_time=time(15, 30)),
    )
    assert "endDate" not in _labels(store, created.note_id)  # minimal storage
    result = store.get_event(cal, uid).to_result(ZoneInfo("Asia/Shanghai"))
    assert result["end_date"] == "2026-11-11"  # not None
    assert result["end"] == "2026-11-11T15:30:00+08:00"

    store.delete_event(cal, uid)


def test_delete_is_idempotent(temp_calendar):
    store = temp_calendar.store
    cal = store.resolve("TestCal")
    uid = new_uid()
    store.create_event(cal, Event(uid=uid, title="doomed", start_date=date(2026, 12, 1)))

    assert store.delete_event(cal, uid) is True
    assert store.delete_event(cal, uid) is False  # already gone, not an error
    with pytest.raises(CalendarError):
        store.get_event(cal, uid)


def test_unknown_uid_raises_with_context(temp_calendar):
    with pytest.raises(CalendarError) as err:
        temp_calendar.store.get_event(temp_calendar.store.resolve("TestCal"), "does-not-exist")
    assert "does-not-exist" in str(err.value)

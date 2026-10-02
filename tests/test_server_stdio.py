"""End-to-end tests: talk to the real MCP server over stdio, as a client would."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_get_event",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "calendar_get_upcoming_events",
    "calendar_upsert_events",
    "calendar_delete_events",
}


def _run(env: dict, calls: list[tuple[str, dict]]) -> tuple[list, list]:
    """Start the server over stdio, list tools, run each call, return the results."""

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "trilium_calendar_mcp.server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                results = [await session.call_tool(name, args) for name, args in calls]
                return tools, results

    return asyncio.run(main())


def _payload(result) -> dict:
    assert not _is_error(result), _text(result)
    return json.loads(_text(result))


def _is_error(result) -> bool:
    """mcp >= 2 exposes snake_case fields; earlier versions used camelCase."""
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def _schema(tool) -> dict:
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {}) or {}


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def test_tools_are_exposed_with_schemas(temp_calendar):
    tools, _ = _run(temp_calendar.env(), [])
    by_name = {tool.name: tool for tool in tools}
    assert EXPECTED_TOOLS.issubset(by_name)
    for name in EXPECTED_TOOLS:
        tool = by_name[name]
        assert tool.description and len(tool.description) > 40, f"{name} lacks a description"
        assert "properties" in _schema(tool), f"{name} has no argument schema"
    # the create tool must accept the Nextcloud-shaped argument names
    props = _schema(by_name["calendar_create_event"])["properties"]
    for arg in ("calendar_name", "title", "start_datetime", "end_datetime", "all_day", "description"):
        assert arg in props


def test_calendar_name_is_discoverable(temp_calendar):
    """The user says "TestCal": the agent must be able to learn that name."""
    _, results = _run(temp_calendar.env(), [("calendar_list_calendars", {})])
    payload = _payload(results[0])
    assert payload["success"] is True

    calendars = {c["name"]: c for c in payload["calendars"]}
    assert "TestCal" in calendars
    assert calendars["TestCal"]["note_id"] == temp_calendar.note_id
    assert calendars["TestCal"]["default"] is True
    assert calendars["TestCal"]["timezone"] == temp_calendar.settings.timezone


def test_full_lifecycle_over_mcp(temp_calendar):
    title = "MCP lifecycle event"
    calls = [
        ("calendar_create_event", {"calendar_name": "TestCal", "title": title,
                                   "start_datetime": "2026-12-24T20:00:00",
                                   "end_datetime": "2026-12-24T22:00:00",
                                   "description": "line one\nline two", "categories": "party,game"}),
        ("calendar_list_events", {"calendar_name": "TestCal", "start_date": "2026-12-01",
                                  "end_date": "2026-12-31"}),
    ]
    _, results = _run(temp_calendar.env(), calls)

    created = _payload(results[0])
    assert created["success"] is True
    uid = created["event"]["uid"]
    assert created["event"]["start"] == "2026-12-24T20:00:00+08:00"
    assert created["event"]["calendar_name"] == "TestCal"

    listed = _payload(results[1])
    assert [e["uid"] for e in listed["events"]] == [uid]
    assert listed["events"][0]["description"].strip() == "line one\nline two"
    assert listed["events"][0]["categories"] == ["party", "game"]

    calls = [
        ("calendar_get_event", {"calendar_name": "TestCal", "event_uid": uid}),
        ("calendar_update_event", {"event_uid": uid, "title": "renamed", "all_day": False,
                                   "start_datetime": "2026-12-25T09:00:00", "end_datetime": ""}),
        ("calendar_create_event", {"calendar_name": "TestCal", "title": "ignored",
                                   "start_datetime": "2026-12-26", "event_uid": uid}),
        ("calendar_delete_event", {"event_uid": uid, "calendar_name": "TestCal"}),
        ("calendar_delete_event", {"event_uid": uid, "calendar_name": "TestCal"}),
    ]
    _, results = _run(temp_calendar.env(), calls)
    assert _payload(results[0])["event"]["uid"] == uid
    assert _payload(results[1])["event"]["title"] == "renamed"

    # reusing the uid updates instead of duplicating
    reused = _payload(results[2])
    assert reused["message"].endswith("'TestCal'.") and "updated" in reused["message"]

    assert _payload(results[3])["success"] is True
    assert "not found" in _payload(results[4])["message"]  # deleting twice is safe


def test_multiple_calendars_can_be_configured(temp_calendar):
    env = dict(temp_calendar.env())
    env["TRILIUM_CALENDARS"] = f"{temp_calendar.note_id}:Gaming,{temp_calendar.note_id}:Second"
    # duplicate ids collapse to one; this asserts the alias parsing path runs
    _, results = _run(env, [("calendar_list_calendars", {})])
    payload = _payload(results[0])
    assert payload["total_count"] >= 1
    assert payload["calendars"][0]["name"] == "Gaming"


def test_errors_are_reported_to_the_agent(temp_calendar):
    calls = [
        ("calendar_get_event", {"calendar_name": "Gaming", "event_uid": "x"}),
        ("calendar_create_event", {"calendar_name": "TestCal", "title": "bad",
                                   "start_datetime": "not-a-date"}),
        ("calendar_list_events", {"calendar_name": "TestCal", "start_date": "2026-12-31",
                                  "end_date": "2026-01-01"}),
    ]
    _, results = _run(temp_calendar.env(), calls)

    text = _text(results[0])
    assert _is_error(results[0])
    assert "Gaming" in text and "TestCal" in text  # tells the agent what to use instead

    assert _is_error(results[1])
    assert "start_datetime" in _text(results[1])

    assert _is_error(results[2])
    assert "before" in _text(results[2])


def test_bulk_upsert_and_delete(temp_calendar):
    events = [
        {"key": "bulk-one", "title": "Bulk one", "start_datetime": "2027-01-05",
         "end_datetime": "2027-01-09", "description": "first", "categories": "a,b"},
        {"key": "bulk-two", "title": "Bulk two", "start_datetime": "2027-02-05T10:00:00",
         "end_datetime": "2027-02-05T11:00:00"},
    ]
    _, results = _run(temp_calendar.env(), [("calendar_upsert_events",
                                             {"calendar_name": "TestCal", "events": events})])
    payload = _payload(results[0])
    assert payload["created"] == 2 and payload["updated"] == 0
    uids = [r["uid"] for r in payload["results"]]

    # the same list again (same keys) updates in place, without duplicating
    _, results = _run(temp_calendar.env(), [("calendar_upsert_events",
                                             {"calendar_name": "TestCal", "events": events})])
    second = _payload(results[0])
    assert second["updated"] == 2 and second["created"] == 0
    assert [r["uid"] for r in second["results"]] == uids  # stable uid from the key

    _, results = _run(temp_calendar.env(), [
        ("calendar_list_events", {"calendar_name": "TestCal", "start_date": "2027-01-01",
                                  "end_date": "2027-01-31"}),
        ("calendar_delete_events", {"calendar_name": "TestCal", "event_uids": uids + ["ghost"]}),
    ])
    listed = _payload(results[0])
    assert [e["title"] for e in listed["events"]] == ["Bulk one"]
    assert listed["events"][0]["end"] == "2027-01-09"  # inclusive end preserved

    deleted = _payload(results[1])
    assert deleted["deleted"] == 2
    assert [r["deleted"] for r in deleted["results"]] == [True, True, False]


def test_missing_start_datetime_is_rejected(temp_calendar):
    _, results = _run(temp_calendar.env(), [("calendar_create_event", {"title": "no start"})])
    assert _is_error(results[0])
    assert "start_datetime" in _text(results[0])

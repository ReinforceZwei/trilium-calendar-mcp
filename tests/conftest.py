"""Shared fixtures.

Integration tests need a live Trilium instance:

    export TRILIUM_URL=https://trilium.example.com
    export TRILIUM_ETAPI_TOKEN=...
    pytest            # unit only if the env vars are missing, unit + integration if set

They create a throwaway calendar note under ``root``, use it, and delete it
again (which cascades to every event they created).
"""

from __future__ import annotations

import os

import pytest

from trilium_calendar_mcp.config import CalendarSpec, Settings
from trilium_calendar_mcp.store import CalendarStore

TEST_CALENDAR_TITLE = "__trilium_calendar_mcp_test__"


def live_env() -> dict:
    return {
        "TRILIUM_URL": os.environ.get("TRILIUM_URL", ""),
        "TRILIUM_ETAPI_TOKEN": os.environ.get("TRILIUM_ETAPI_TOKEN", ""),
    }


def live_available() -> bool:
    env = live_env()
    return bool(env["TRILIUM_URL"] and env["TRILIUM_ETAPI_TOKEN"])


requires_live = pytest.mark.skipif(
    not live_available(), reason="needs TRILIUM_URL and TRILIUM_ETAPI_TOKEN"
)


class TempCalendar:
    def __init__(self, note_id: str, settings: Settings, store: CalendarStore):
        self.note_id = note_id
        self.settings = settings
        self.store = store

    def env(self) -> dict:
        """Environment for a subprocess running the MCP server against this calendar."""
        env = dict(os.environ)
        env.update(
            {
                "TRILIUM_URL": self.settings.url,
                "TRILIUM_ETAPI_TOKEN": self.settings.token,
                "TRILIUM_CALENDARS": f"{self.note_id}:TestCal",
                "TRILIUM_TIMEZONE": self.settings.timezone,
                "MCP_TRANSPORT": "stdio",
            }
        )
        return env


@pytest.fixture(scope="session")
def temp_calendar():
    if not live_available():
        pytest.skip("needs TRILIUM_URL and TRILIUM_ETAPI_TOKEN")

    from trilium_py.client import ETAPI

    url, token = live_env()["TRILIUM_URL"].rstrip("/"), live_env()["TRILIUM_ETAPI_TOKEN"]
    client = ETAPI(url, token)

    created = client.create_note(
        parentNoteId="root", title=TEST_CALENDAR_TITLE, type="book", content="<p></p>"
    )
    note_id = (created.get("note") or {}).get("noteId")
    assert note_id, f"could not create the test calendar: {created!r}"
    # Mimic a real calendar collection: an inherited template whose body would
    # otherwise land in every event note.
    client.create_attribute(note_id, "relation", "template", "_template_calendar", True)

    settings = Settings(
        url=url,
        token=token,
        calendars=[CalendarSpec(note_id=note_id, alias="TestCal", default=True)],
    )
    settings.timezone = os.environ.get("TRILIUM_TIMEZONE", "Asia/Shanghai")
    store = CalendarStore(settings)
    try:
        yield TempCalendar(note_id, settings, store)
    finally:
        client.delete_note(note_id)

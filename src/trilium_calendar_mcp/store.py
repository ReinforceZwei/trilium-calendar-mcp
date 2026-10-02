"""Trilium backend: notes-as-events, over ETAPI.

Design notes that shaped this code (each one cost real debugging time):

* ``create-note`` ignores a passed ``content`` when the parent collection carries
  an inheritable ``~template``: Trilium applies the template and the template's
  body wins. Content is therefore always written in a second step.
* ``create-note`` rejects an inline ``attributes`` property, so labels are
  attached one call at a time.
* Event lookup is a scoped ETAPI search (``note.parents.noteId = ... and
  #startDate``) because a search result already carries the note's attributes —
  walking children would cost one request per event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .config import CalendarSpec, Settings
from .errors import CalendarError, TriliumError
from .event import DATE_LABEL, UID_LABEL, Event, MANAGED_LABELS
from .htmlconv import html_to_text, text_to_html

log = logging.getLogger(__name__)


@dataclass
class Calendar:
    spec: CalendarSpec
    title: str
    note_id: str
    timezone: str
    color: str = ""
    description: str = ""
    event_count: int | None = None

    @property
    def name(self) -> str:
        """The name the agent should use: configured alias, else the note title."""
        return self.spec.alias or self.title

    def to_result(self) -> dict:
        return {
            "name": self.name,
            "note_id": self.note_id,
            "title": self.title,
            "alias": self.spec.alias,
            "default": self.spec.default,
            "timezone": self.timezone,
            "color": self.color,
            "description": self.description,
            "event_count": self.event_count,
        }


def _make_session(settings: Settings):
    """A requests session carrying TLS/timeout policy.

    trilium-py calls ``requests.get(...)`` module-level helpers, which take no
    timeout and always verify TLS; the session below is injected in their place
    so both are configurable (self-hosted Trilium with a private CA, or a
    backend that stops answering).
    """
    import requests

    session = requests.Session()

    class Shim:
        def _call(self, method, url, **kwargs):
            kwargs.setdefault("timeout", settings.timeout)
            kwargs.setdefault("verify", settings.ca_bundle or settings.verify_ssl)
            return session.request(method, url, **kwargs)

        def get(self, url, **kwargs):
            return self._call("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self._call("POST", url, **kwargs)

        def put(self, url, **kwargs):
            return self._call("PUT", url, **kwargs)

        def patch(self, url, **kwargs):
            return self._call("PATCH", url, **kwargs)

        def delete(self, url, **kwargs):
            return self._call("DELETE", url, **kwargs)

        exceptions = requests.exceptions

    return Shim()


class CalendarStore:
    """Calendar operations backed by Trilium notes."""

    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self._client = client
        self._calendar_cache: dict[str, Calendar] | None = None

    # ── plumbing ──────────────────────────────────────────────────────────

    @property
    def client(self):
        if self._client is None:
            from trilium_py.client import ETAPI
            import trilium_py.client as trilium_client

            trilium_client.requests = _make_session(self.settings)
            self._client = ETAPI(self.settings.url, self.settings.token)
            # trilium-py builds headers as {"Authorization": self.token}; ETAPI
            # expects the raw token here.
        return self._client

    def _ok(self, what: str, result):
        if isinstance(result, dict) and result.get("code") and result.get("status"):
            raise TriliumError(
                f"Trilium rejected {what}: {result.get('code')} — {result.get('message')}"
            )
        return result

    def _note(self, note_id: str) -> dict:
        note = self._ok(f"read note {note_id}", self.client.get_note(note_id))
        if not isinstance(note, dict) or not note.get("noteId"):
            raise TriliumError(f"Note {note_id} not found or unreadable.")
        return note

    def _content(self, note_id: str) -> str:
        try:
            return self.client.get_note_content(note_id) or ""
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not read content of %s: %s", note_id, exc)
            return ""

    def _search(self, query: str, limit: int | None = None) -> list[dict]:
        result = self._ok(
            f"search {query!r}",
            self.client.search_note(
                query,
                limit=limit or self.settings.search_limit,
                fastSearch="true",
            ),
        )
        if not isinstance(result, dict):
            raise TriliumError(f"Unexpected search response: {result!r}")
        return result.get("results") or []

    # ── calendars ─────────────────────────────────────────────────────────

    def calendars(self, refresh: bool = False) -> list[Calendar]:
        if self._calendar_cache is None or refresh:
            items: list[Calendar] = []
            for spec in self.settings.calendars:
                note = self._note(spec.note_id)
                labels = {
                    a["name"]: a.get("value", "")
                    for a in (note.get("attributes") or [])
                    if a.get("type") == "label" and not a.get("isInheritable")
                }
                items.append(
                    Calendar(
                        spec=spec,
                        title=note.get("title") or spec.note_id,
                        note_id=spec.note_id,
                        timezone=labels.get("calendar:timezone") or labels.get("caldavTimezone") or self.settings.timezone,
                        color=labels.get("color", ""),
                        description=labels.get("description", ""),
                    )
                )
            self._calendar_cache = {c.note_id: c for c in items}
        return list(self._calendar_cache.values())

    def resolve(self, calendar_name: str | None) -> Calendar:
        """Resolve an agent-supplied calendar name to a configured calendar.

        Accepts, case-insensitively: the configured name/alias, the Trilium note
        title, or the note id itself.
        """
        cals = self.calendars()
        if not cals:
            raise CalendarError("No calendars are configured (TRILIUM_CALENDARS is empty).")
        if calendar_name is None or not str(calendar_name).strip():
            default = next((c for c in cals if c.spec.default), cals[0])
            return default

        needle = str(calendar_name).strip()
        for cal in cals:
            if needle == cal.note_id:
                return cal
        lowered = needle.lower()
        for key in ("name", "title"):
            for cal in cals:
                if getattr(cal, key).lower() == lowered:
                    return cal
        available = ", ".join(f"{c.name} ({c.note_id})" for c in cals)
        raise CalendarError(f"Unknown calendar {calendar_name!r}. Available calendars: {available}.")

    # ── events: read ──────────────────────────────────────────────────────

    def _event_notes(self, calendar: Calendar) -> list[dict]:
        query = f'note.parents.noteId = "{calendar.note_id}" and #{DATE_LABEL}'
        return self._search(query)

    def owner_of(self, note: dict) -> Calendar:
        """The configured calendar a note belongs to."""
        parents = note.get("parentNoteIds") or []
        for cal in self.calendars():
            if cal.note_id in parents:
                return cal
        raise CalendarError(
            f"Note {note.get('noteId')} is not inside any configured calendar."
        )

    def _events(self, calendar: Calendar, notes: list[dict] | None = None) -> list[Event]:
        events: list[Event] = []
        for note in notes if notes is not None else self._event_notes(calendar):
            try:
                events.append(
                    Event.from_note(
                        note,
                        content="",
                        calendar_name=calendar.name,
                        calendar_note_id=calendar.note_id,
                        html_to_text=lambda _raw: "",
                    )
                )
            except CalendarError:
                log.warning("skipping note %s: not an event", note.get("noteId"))
        return events

    def list_events(
        self,
        calendar: Calendar,
        start: date | None = None,
        end: date | None = None,
        limit: int = 100,
        title_contains: str | None = None,
        categories: str | None = None,
        location_contains: str | None = None,
        include_descriptions: bool = True,
    ) -> list[Event]:
        events = self._events(calendar)
        if start:
            events = [e for e in events if e.last_date >= start]
        if end:
            events = [e for e in events if e.start_date <= end]
        if title_contains:
            needle = title_contains.lower()
            events = [e for e in events if needle in e.title.lower()]
        if location_contains:
            needle = location_contains.lower()
            events = [e for e in events if needle in (e.location or "").lower()]
        if categories:
            wanted = {c.strip().lower() for c in categories.split(",") if c.strip()}
            events = [e for e in events if wanted & {c.lower() for c in e.categories}]

        tz = self._tz(calendar)
        events.sort(key=lambda e: (e.start_datetime(tz), e.title))
        if limit and limit > 0:
            events = events[:limit]
        if include_descriptions:
            for event in events:
                if event.note_id:
                    event.description = html_to_text(self._content(event.note_id))
        return events

    def find_event_note(self, calendar: Calendar | None, uid: str) -> dict | None:
        """Find the note holding ``uid``, scoped to configured calendars only."""
        if not uid or '"' in uid:
            raise CalendarError(f"Invalid event uid {uid!r}.")
        if calendar is not None:
            return self._find_in(calendar, uid)
        for cal in self.calendars():
            found = self._find_in(cal, uid)
            if found is not None:
                return found
        return None

    def _find_in(self, calendar: Calendar, uid: str) -> dict | None:
        notes = self._search(f'#{UID_LABEL} = "{uid}"')
        return next(
            (n for n in notes if calendar.note_id in (n.get("parentNoteIds") or [])),
            None,
        )

    def get_event(self, calendar: Calendar | None, uid: str, *, with_content: bool = True) -> Event:
        note = self.find_event_note(calendar, uid)
        if note is None:
            where = f" in calendar {calendar.name!r}" if calendar else ""
            raise CalendarError(f"No event with uid {uid!r}{where}.")
        content = self._content(note["noteId"]) if with_content else ""
        return self._to_event(note, calendar, content)

    def _to_event(self, note: dict, calendar: Calendar | None, content: str = "") -> Event:
        parent_ids = note.get("parentNoteIds") or []
        owner = calendar
        if owner is None and parent_ids:
            owner = next((c for c in self.calendars() if c.note_id in parent_ids), None)
        return Event.from_note(
            note,
            content=content,
            calendar_name=owner.name if owner else None,
            calendar_note_id=owner.note_id if owner else (parent_ids[0] if parent_ids else None),
            html_to_text=html_to_text,
        )

    def upcoming(
        self, calendar: Calendar | None, days_ahead: int, limit: int
    ) -> list[Event]:
        anchor = date.today()
        horizon = anchor + timedelta(days=max(days_ahead, 0))
        targets = self.calendars() if calendar is None else [calendar]
        events: list[Event] = []
        for cal in targets:
            events.extend(self.list_events(cal, start=anchor, end=horizon, limit=0, include_descriptions=False))
        tz = self._tz(calendar)
        events.sort(key=lambda e: e.start_datetime(tz))
        selected = events[:limit] if limit and limit > 0 else events
        for event in selected:
            if event.note_id:
                event.description = html_to_text(self._content(event.note_id))
        return selected

    def _tz(self, calendar: Calendar | None) -> ZoneInfo:
        from .config import zone_of

        return zone_of(calendar.timezone if calendar else self.settings.timezone)

    # ── events: write ─────────────────────────────────────────────────────

    def create_event(self, calendar: Calendar, event: Event) -> Event:
        event.validate()
        created = self._ok(
            "create note",
            self.client.create_note(
                parentNoteId=calendar.note_id,
                title=event.title,
                type="text",
                # ETAPI rejects an empty/absent content ("Note content must be
                # set"); the real body is written immediately after.
                content="<p></p>",
            ),
        )
        note = (created or {}).get("note") or {}
        note_id = note.get("noteId")
        if not note_id:
            raise TriliumError(f"Could not create event note: {created!r}")

        self._write_labels(note_id, event.labels())
        # Always overwrite the body: an inherited collection template may have
        # filled it in, and the caller's description is the source of truth.
        self._write_content(note_id, event.description)

        event.note_id = note_id
        event.calendar_name = calendar.name
        event.calendar_note_id = calendar.note_id
        return event

    def _write_labels(self, note_id: str, labels: dict[str, str]) -> None:
        for name, value in labels.items():
            self._ok(
                f"set #{name}",
                self.client.create_attribute(note_id, "label", name, value, False),
            )

    def _write_content(self, note_id: str, description: str) -> None:
        if not self.client.update_note_content(note_id, text_to_html(description)):
            raise TriliumError(f"Could not write content of note {note_id}.")

    def update_event(self, calendar: Calendar, uid: str, changes: dict) -> Event:
        """Apply ``changes`` to an event.

        Only the keys present are touched; a key set to None clears that field.
        Labels that are no longer needed (for example ``#endDate`` after an
        all-day event becomes a single timed one) are removed.
        """
        note = self.find_event_note(calendar, uid)
        if note is None:
            raise CalendarError(f"No event with uid {uid!r} in calendar {calendar.name!r}.")
        note_id = note["noteId"]
        current = Event.from_note(
            note,
            content="",
            calendar_name=calendar.name,
            calendar_note_id=calendar.note_id,
            html_to_text=lambda _raw: "",
        )

        for key, value in changes.items():
            # Keys present in ``changes`` are applied verbatim: None clears a
            # field (e.g. removing #endDate when an all-day event becomes timed).
            setattr(current, key, value)
        current.validate()

        if note.get("title") != current.title:
            self._ok("rename note", self.client.patch_note(note_id, title=current.title))

        desired = current.labels()
        existing: dict[str, dict] = {}
        for attr in note.get("attributes") or []:
            if attr.get("type") != "label" or attr.get("isInheritable"):
                continue
            name = attr.get("name") or ""
            if name in MANAGED_LABELS:
                existing[name] = attr

        for name, value in desired.items():
            attr = existing.get(name)
            if attr is None:
                self._ok(
                    f"set #{name}",
                    self.client.create_attribute(note_id, "label", name, value, False),
                )
            elif (attr.get("value") or "") != value:
                self._ok(
                    f"update #{name}",
                    self.client.patch_attribute(attr["attributeId"], value),
                )
        for name, attr in existing.items():
            if name not in desired:
                self._ok(
                    f"remove #{name}",
                    self.client.delete_attribute(attr["attributeId"]),
                )

        if changes.get("description") is not None:
            self._write_content(note_id, current.description)

        current.note_id = note_id
        return current

    def delete_event(self, calendar: Calendar, uid: str) -> bool:
        note = self.find_event_note(calendar, uid)
        if note is None:
            return False
        if not self.client.delete_note(note["noteId"]):
            raise TriliumError(f"Trilium refused to delete note {note['noteId']}.")
        return True

    def upsert_event(self, calendar: Calendar, event: Event) -> tuple[Event, bool]:
        """Create or update by uid. Returns (event, created)."""
        if event.uid:
            note = self.find_event_note(calendar, event.uid)
            if note is not None:
                updated = self.update_event(calendar, event.uid, self.changes_from(event))
                return updated, False
        return self.create_event(calendar, event), True

    @staticmethod
    def changes_from(event: Event) -> dict:
        return {
            "title": event.title,
            "start_date": event.start_date,
            "end_date": event.end_date,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "description": event.description,
            "location": event.location,
            "categories": event.categories,
            "color": event.color,
            "recurrence": event.recurrence,
        }

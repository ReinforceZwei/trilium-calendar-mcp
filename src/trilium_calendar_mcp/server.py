"""MCP server: calendar tools for AI agents, backed by Trilium notes.

The tool names and arguments deliberately mirror the Nextcloud Calendar MCP
(``nc_calendar_*`` -> ``calendar_*``) so an agent already trained on that
toolset needs only a rename, not a re-learn.
"""

from __future__ import annotations

import functools
import logging
import sys
from datetime import date, time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import Settings, load_settings, zone_of
from .errors import CalendarError, TriliumError
from .event import Event, new_uid, parse_date_value, parse_datetime_value, stable_uid
from .store import Calendar, CalendarStore

log = logging.getLogger("trilium_calendar_mcp")

INSTRUCTIONS = """\
Calendar tools backed by Trilium notes.

Calendars are configured server-side; call calendar_list_calendars first to see
the available names, then pass that name as `calendar_name` (it is optional
only when a single default calendar is configured).

Conventions:
- `start_datetime` / `end_datetime` accept `YYYY-MM-DD` (all-day) or ISO
  datetimes such as `2026-07-08T11:00:00` or `2026-07-08T03:00:00Z`. Naive
  values are read in the calendar's timezone; offset-aware values are converted.
- For all-day events `end_datetime` is the LAST day of the event (inclusive),
  and a date-only `start_datetime` means a single all-day event.
- Every event carries a stable `uid`. Pass one back on create (or use
  calendar_upsert_events) to update instead of duplicating — safe for re-runs.
- Descriptions are stored as plain text; line breaks are preserved.
- Not stored by Trilium: attendees, reminders/alarms, status, priority,
  privacy, url. Passing them is accepted but reported in `ignored_fields`.
"""


def _translate(fn):
    """Turn domain errors into MCP tool errors with the message intact."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (CalendarError, TriliumError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _split_categories(value: str | None) -> list[str]:
    if not value:
        return []
    return [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]


def _apply_recurrence_end(rule: str, end_date_str: str) -> str:
    """Append UNTIL to an RRULE, mirroring the Nextcloud tool's contract."""
    if not rule:
        raise CalendarError("recurrence_end_date requires recurrence_rule.")
    upper = rule.upper()
    if "UNTIL=" in upper or "COUNT=" in upper:
        raise CalendarError(
            "recurrence_rule already carries UNTIL or COUNT; remove recurrence_end_date."
        )
    try:
        until = parse_date_value(end_date_str, "recurrence_end_date")
    except CalendarError as exc:
        raise CalendarError(
            f"Invalid recurrence_end_date {end_date_str!r}; expected an ISO date such as 2026-12-31."
        ) from exc
    return f"{rule.rstrip(';')};UNTIL={until.strftime('%Y%m%d')}T235959Z"


def _parse_rrule(rule: str | None, end_date_str: str | None) -> str:
    """Normalize an RRULE: accept with or without the 'RRULE:' prefix."""
    if not rule:
        return ""
    cleaned = rule.strip()
    if cleaned.upper().startswith("RRULE:"):
        cleaned = cleaned[6:]
    if end_date_str:
        cleaned = _apply_recurrence_end(cleaned, end_date_str)
    return cleaned


class Toolbox:
    """Thin adapter between MCP tool arguments and the CalendarStore."""

    def __init__(self, settings: Settings, store: CalendarStore | None = None):
        self.settings = settings
        self.store = store or CalendarStore(settings)

    # ── helpers ───────────────────────────────────────────────────────────

    def calendar(self, name: str | None) -> Calendar:
        return self.store.resolve(name)

    def tz_for(self, event: Event) -> ZoneInfo:
        """Timezone to render an event in: its own calendar's, else the default."""
        if event.calendar_note_id:
            for cal in self.store.calendars():
                if cal.note_id == event.calendar_note_id:
                    return zone_of(cal.timezone)
        return zone_of(self.settings.timezone)

    def build_event(
        self,
        calendar: Calendar,
        *,
        title: str,
        start_datetime: str,
        end_datetime: str = "",
        all_day: bool = False,
        description: str = "",
        location: str = "",
        categories: str = "",
        timezone: str = "",
        color: str = "",
        recurrence_rule: str = "",
        recurrence_end_date: str = "",
        recurring: bool | None = None,
        uid: str = "",
    ) -> Event:
        cal_tz = zone_of(calendar.timezone)
        source_tz = timezone or None
        start_date, start_time = parse_datetime_value(
            start_datetime, cal_tz, source_tz, "start_datetime"
        )
        if all_day:
            start_time = None

        end_date = None
        end_time = None
        if end_datetime:
            end_date, end_time = parse_datetime_value(
                end_datetime, cal_tz, source_tz, "end_datetime"
            )
            if start_time is None:
                end_time = None  # all-day: only the last day matters

        rule = _parse_rrule(recurrence_rule, recurrence_end_date or None)
        if recurring is False:
            rule = ""

        return Event(
            uid=uid or new_uid(),
            title=title,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            description=description or "",
            location=location or "",
            categories=_split_categories(categories),
            color=color or "",
            recurrence=rule,
        )


def build_server(settings: Settings, store: CalendarStore | None = None) -> MCPServer:
    toolbox = Toolbox(settings, store)
    server = MCPServer(
        name="trilium-calendar",
        title="Trilium Calendar",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool(name="calendar_list_calendars")
    @_translate
    def calendar_list_calendars() -> dict:
        """List the calendars this server exposes.

        Returns each calendar's `name` (use this as `calendar_name` in other
        tools), its Trilium `note_id`, note `title`, timezone, colour and the
        current number of events. Start here: calendar names are how the user
        refers to calendars (e.g. "Gaming"), and they are what the other tools
        accept.
        """
        calendars = toolbox.store.calendars(refresh=True)
        results = []
        for cal in calendars:
            cal.event_count = len(toolbox.store.list_events(cal, limit=0, include_descriptions=False))
            results.append(cal.to_result())
        return {"success": True, "calendars": results, "total_count": len(results)}

    @server.tool(name="calendar_list_events")
    @_translate
    def calendar_list_events(
        calendar_name: str = "",
        start_date: str = "",
        end_date: str = "",
        limit: int = 100,
        title_contains: str = "",
        categories: str = "",
        location_contains: str = "",
        include_descriptions: bool = True,
        search_all_calendars: bool = False,
    ) -> dict:
        """List events, optionally filtered by date range and text.

        Args:
            calendar_name: Calendar to read; omit to use the default calendar.
            start_date: Earliest day to return, `YYYY-MM-DD` (events overlapping
                the range are included).
            end_date: Latest day to return, `YYYY-MM-DD`.
            limit: Maximum number of events to return (0 = no limit).
            title_contains: Only events whose title contains this text.
            categories: Comma-separated categories; matches events carrying any of them.
            location_contains: Only events whose location contains this text.
            include_descriptions: Fetch event bodies (one extra request per event).
            search_all_calendars: Search every configured calendar instead of one.
        """
        if search_all_calendars:
            targets = toolbox.store.calendars()
        else:
            targets = [toolbox.calendar(calendar_name or None)]

        start = parse_date_value(start_date, "start_date") if start_date else None
        end = parse_date_value(end_date, "end_date") if end_date else None
        if start and end and end < start:
            raise CalendarError(
                f"end_date {end.isoformat()} is before start_date {start.isoformat()}."
            )

        events: list[Event] = []
        for cal in targets:
            events.extend(
                toolbox.store.list_events(
                    cal,
                    start=start,
                    end=end,
                    limit=0,
                    title_contains=title_contains or None,
                    categories=categories or None,
                    location_contains=location_contains or None,
                    include_descriptions=include_descriptions,
                )
            )

        events.sort(key=lambda e: (e.start_date, e.start_time or time.min, e.title))
        if limit and limit > 0:
            events = events[:limit]
        return {
            "success": True,
            "events": [e.to_result(toolbox.tz_for(e)) for e in events],
            "total_found": len(events),
            "calendar_name": calendar_name or (targets[0].name if len(targets) == 1 else None),
        }

    @server.tool(name="calendar_get_event")
    @_translate
    def calendar_get_event(calendar_name: str = "", event_uid: str = "") -> dict:
        """Get one event by its uid, including its description.

        Args:
            calendar_name: Calendar to look in; omit to search all calendars.
            event_uid: The event's `uid` (from calendar_list_events).
        """
        if not event_uid:
            raise CalendarError("event_uid is required.")
        calendar = toolbox.calendar(calendar_name) if calendar_name else None
        event = toolbox.store.get_event(calendar, event_uid)
        return {"success": True, "event": event.to_result(toolbox.tz_for(event))}

    @server.tool(name="calendar_create_event")
    @_translate
    def calendar_create_event(
        title: str,
        start_datetime: str,
        calendar_name: str = "",
        end_datetime: str = "",
        all_day: bool = False,
        description: str = "",
        location: str = "",
        categories: str = "",
        timezone: str = "",
        color: str = "",
        recurrence_rule: str = "",
        recurrence_end_date: str = "",
        recurring: bool | None = None,
        event_uid: str = "",
        status: str = "",
        priority: int | None = None,
        privacy: str = "",
        attendees: str = "",
        url: str = "",
        reminder_minutes: int | None = None,
        reminder_email: bool | None = None,
        reminders: list[dict] | None = None,
    ) -> dict:
        """Create a calendar event.

        Args:
            title: Event title.
            start_datetime: `YYYY-MM-DD` for an all-day event, or an ISO datetime
                such as `2026-07-08T11:00:00` (naive = calendar timezone) /
                `2026-07-08T03:00:00Z` (converted to calendar timezone).
            calendar_name: Calendar to create in; omit for the default calendar.
            end_datetime: ISO datetime end, or for all-day events the LAST day
                (inclusive). Empty = same day / no end.
            all_day: Force an all-day event (times are ignored).
            description: Plain text; line breaks are preserved.
            location: Free text location.
            categories: Comma-separated categories/tags.
            timezone: IANA timezone used to interpret naive datetimes.
            color: Colour name or hex.
            recurrence_rule: RRULE body, with or without the `RRULE:` prefix.
            recurrence_end_date: `YYYY-MM-DD` that bounds the series (writes UNTIL).
            recurring: Set False to force a non-recurring event.
            event_uid: Reuse an existing uid (updates that event instead of
                creating a duplicate) — use it to make retries idempotent.
            status, priority, privacy, attendees, url, reminder_minutes,
            reminder_email, reminders: accepted for compatibility but NOT stored
                by Trilium; they are echoed back in `ignored_fields`.
        """
        calendar = toolbox.calendar(calendar_name or None)
        event = toolbox.build_event(
            calendar,
            title=title,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            all_day=all_day,
            description=description,
            location=location,
            categories=categories,
            timezone=timezone,
            color=color,
            recurrence_rule=recurrence_rule,
            recurrence_end_date=recurrence_end_date,
            recurring=recurring,
            uid=event_uid,
        )

        ignored = [
            name
            for name, value in (
                ("status", status),
                ("priority", priority),
                ("privacy", privacy),
                ("attendees", attendees),
                ("url", url),
                ("reminder_minutes", reminder_minutes),
                ("reminder_email", reminder_email),
                ("reminders", reminders),
            )
            if value not in (None, "", 0, False)
        ]

        existing = toolbox.store.find_event_note(calendar, event.uid) if event_uid else None
        if existing is not None:
            saved = toolbox.store.update_event(calendar, event.uid, toolbox.store.changes_from(event))
            verb = "updated"
        else:
            saved = toolbox.store.create_event(calendar, event)
            verb = "created"
        return {
            "success": True,
            "message": f"Event {verb} in calendar {calendar.name!r}.",
            "event": saved.to_result(toolbox.tz_for(saved)),
            "ignored_fields": ignored,
        }

    @server.tool(name="calendar_update_event")
    @_translate
    def calendar_update_event(
        event_uid: str,
        calendar_name: str = "",
        title: str | None = None,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        location: str | None = None,
        categories: str | None = None,
        timezone: str | None = None,
        color: str | None = None,
        recurrence_rule: str | None = None,
        recurrence_end_date: str | None = None,
        recurring: bool | None = None,
    ) -> dict:
        """Update an existing event. Only the arguments you pass are changed.

        The returned `event` reflects what is now stored: if you do not pass
        `description`, the response carries the existing body (not an empty
        string), so you do not need to re-fetch to confirm it survived.

        Args:
            event_uid: Uid of the event to update.
            calendar_name: Calendar holding the event; omit to search all calendars.
            (other arguments as in calendar_create_event; `end_datetime=""` clears
            the end, `location=""` clears the location, `recurrence_rule=""` or
            `recurring=False` removes the recurrence.)
        """
        calendar = toolbox.calendar(calendar_name) if calendar_name else None
        current = toolbox.store.get_event(calendar, event_uid)
        if calendar is not None:
            owner = calendar
        else:
            note = toolbox.store.find_event_note(None, event_uid)
            if note is None:  # pragma: no cover - get_event already raised
                raise CalendarError(f"No event with uid {event_uid!r}.")
            owner = toolbox.store.owner_of(note)
        tz_name = timezone or owner.timezone
        tz = zone_of(tz_name)

        changes: dict = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        if location is not None:
            changes["location"] = location
        if categories is not None:
            changes["categories"] = _split_categories(categories)
        if color is not None:
            changes["color"] = color
        if recurring is False:
            changes["recurrence"] = ""
        if recurrence_rule is not None:
            changes["recurrence"] = _parse_rrule(recurrence_rule, recurrence_end_date or None)
        elif recurrence_end_date:
            changes["recurrence"] = _parse_rrule(current.recurrence, recurrence_end_date)

        if start_datetime is not None:
            start_date, start_time = parse_datetime_value(start_datetime, tz, tz_name, "start_datetime")
            changes["start_date"] = start_date
            changes["start_time"] = start_time
        if end_datetime is not None:
            if end_datetime == "":
                changes["end_date"] = None
                changes["end_time"] = None
            else:
                end_date, end_time = parse_datetime_value(end_datetime, tz, tz_name, "end_datetime")
                changes["end_date"] = end_date
                changes["end_time"] = end_time
        if all_day is not None:
            if all_day:
                changes["start_time"] = None
                changes["end_time"] = None
            elif (changes.get("start_time", current.start_time)) is None:
                raise CalendarError(
                    "all_day=False needs a start time: pass start_datetime with a time component."
                )

        updated = toolbox.store.update_event(owner, event_uid, changes)
        return {
            "success": True,
            "message": f"Event updated in calendar {owner.name!r}.",
            "event": updated.to_result(toolbox.tz_for(updated)),
        }

    @server.tool(name="calendar_delete_event")
    @_translate
    def calendar_delete_event(event_uid: str, calendar_name: str = "") -> dict:
        """Delete an event by uid. Deleting an unknown uid succeeds (safe retries).

        Args:
            event_uid: Uid of the event to delete.
            calendar_name: Calendar holding the event; omit to search all calendars.
        """
        if not event_uid:
            raise CalendarError("event_uid is required.")
        targets = [toolbox.calendar(calendar_name)] if calendar_name else toolbox.store.calendars()
        for cal in targets:
            if toolbox.store.delete_event(cal, event_uid):
                return {
                    "success": True,
                    "message": f"Event {event_uid} deleted from calendar {cal.name!r}.",
                }
        return {"success": True, "message": f"Event {event_uid} not found (nothing to delete)."}

    @server.tool(name="calendar_get_upcoming_events")
    @_translate
    def calendar_get_upcoming_events(
        calendar_name: str = "", days_ahead: int = 7, limit: int = 10
    ) -> dict:
        """List events starting between today and N days ahead.

        Args:
            calendar_name: Calendar to read; omit for all calendars.
            days_ahead: How many days ahead to include (today counts as day 0).
            limit: Maximum number of events to return.
        """
        calendar = toolbox.calendar(calendar_name) if calendar_name else None
        events = toolbox.store.upcoming(calendar, days_ahead, limit)
        return {
            "success": True,
            "events": [e.to_result(toolbox.tz_for(e)) for e in events],
            "total_found": len(events),
            "days_ahead": days_ahead,
        }

    @server.tool(name="calendar_upsert_events")
    @_translate
    def calendar_upsert_events(events: list[dict], calendar_name: str = "") -> dict:
        """Create or update many events at once, keyed on `uid` (or on `key`).

        Use this for bulk work (a release calendar for a whole season). Each
        entry is created if its identity is new and updated if it already
        exists, so re-running the same list updates in place instead of
        duplicating. Identity comes from `event_uid`/`uid`, or from `key`: a
        short stable slug (`"wuwa-3.7-banner-1"`) that is hashed into a uid, so
        a scheduled agent can re-publish its list without storing uids.

        Args:
            events: List of event objects. Each accepts the arguments of
                `calendar_create_event` (`title`, `start_datetime`, `end_datetime`,
                `all_day`, `description`, `location`, `categories`, `color`,
                `recurrence_rule`) plus the identity fields `event_uid`/`uid` or
                `key`. `calendar_name` may be set per entry to spread the list
                across calendars. Entries with no identity always create new
                events.
            calendar_name: Default calendar for entries that do not name one.
        """
        if not events:
            return {"success": True, "created": 0, "updated": 0, "results": []}

        results = []
        created = updated = 0
        for index, raw in enumerate(events):
            if not isinstance(raw, dict):
                raise CalendarError(f"events[{index}] must be an object, got {type(raw).__name__}.")
            entry = dict(raw)
            title = entry.pop("title", None) or entry.pop("summary", None)
            if not title:
                raise CalendarError(f"events[{index}] is missing 'title'.")
            start = entry.pop("start_datetime", None) or entry.pop("start", None)
            if not start:
                raise CalendarError(f"events[{index}] is missing 'start_datetime'.")
            target = toolbox.calendar(entry.pop("calendar_name", "") or calendar_name or None)
            for key in ("status", "priority", "privacy", "attendees", "url",
                        "reminder_minutes", "reminder_email", "reminders"):
                entry.pop(key, None)

            uid = entry.pop("event_uid", "") or entry.pop("uid", "") or ""
            stable_key = entry.pop("key", "") or entry.pop("uid_key", "") or ""
            if not uid and stable_key:
                uid = stable_uid(target.note_id, str(stable_key))

            event = toolbox.build_event(
                target,
                title=title,
                start_datetime=start,
                end_datetime=entry.pop("end_datetime", "") or entry.pop("end", "") or "",
                all_day=bool(entry.pop("all_day", False)),
                description=entry.pop("description", "") or "",
                location=entry.pop("location", "") or "",
                categories=entry.pop("categories", "") or "",
                timezone=entry.pop("timezone", "") or "",
                color=entry.pop("color", "") or "",
                recurrence_rule=entry.pop("recurrence_rule", "") or "",
                recurrence_end_date=entry.pop("recurrence_end_date", "") or "",
                uid=uid,
            )
            saved, was_created = toolbox.store.upsert_event(target, event)
            created += int(was_created)
            updated += int(not was_created)
            results.append(
                {
                    "uid": saved.uid,
                    "note_id": saved.note_id,
                    "calendar_name": saved.calendar_name,
                    "title": saved.title,
                    "action": "created" if was_created else "updated",
                }
            )
        return {"success": True, "created": created, "updated": updated, "results": results}

    @server.tool(name="calendar_delete_events")
    @_translate
    def calendar_delete_events(event_uids: list[str], calendar_name: str = "") -> dict:
        """Delete many events by uid in one call.

        Args:
            event_uids: Uids to delete; unknown uids are reported, not an error.
            calendar_name: Calendar to delete from; omit to search all calendars.
        """
        if not event_uids:
            return {"success": True, "deleted": 0, "results": []}
        targets = [toolbox.calendar(calendar_name)] if calendar_name else toolbox.store.calendars()
        results = []
        deleted = 0
        for uid in event_uids:
            found_in = None
            for cal in targets:
                if toolbox.store.delete_event(cal, uid):
                    found_in = cal.name
                    deleted += 1
                    break
            results.append({"uid": uid, "deleted": bool(found_in), "calendar_name": found_in})
        return {"success": True, "deleted": deleted, "results": results}

    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = load_settings()
    except CalendarError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.getLogger().setLevel(settings.log_level)
    names = ", ".join(f"{c.alias or c.note_id} ({c.note_id})" for c in settings.calendars)
    log.info("calendars: %s | timezone: %s | transport: %s", names, settings.timezone, settings.transport)

    server = build_server(settings)
    if settings.transport == "stdio":
        server.run(transport="stdio")
    else:
        from mcp.server.transport_security import TransportSecuritySettings

        from .transport import resolve_transport_security

        policy = resolve_transport_security(
            settings.host, settings.allowed_hosts, settings.allowed_origins
        )
        if policy.enable_dns_rebinding_protection:
            log.info(
                "DNS-rebinding protection: on (%s) - allowed hosts: %s",
                policy.reason,
                ", ".join(policy.allowed_hosts),
            )
        else:
            log.info("DNS-rebinding protection: off (%s)", policy.reason)

        log.info("listening on http://%s:%s%s", settings.host, settings.port, settings.path)
        server.run(
            transport="streamable-http",
            host=settings.host,
            port=settings.port,
            streamable_http_path=settings.path,
            # Always explicit: the SDK's own default depends on the bind address
            # and has changed between versions.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=policy.enable_dns_rebinding_protection,
                allowed_hosts=policy.allowed_hosts,
                allowed_origins=policy.allowed_origins,
            ),
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

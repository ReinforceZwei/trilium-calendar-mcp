"""Event model and the Trilium label conventions it encodes.

How an event is stored on a Trilium note (this is the contract the CalDAV
facade and the Trilium calendar view both understand):

    #startDate    YYYY-MM-DD            always
    #endDate      YYYY-MM-DD            last day, INCLUSIVE (iCal DTEND is exclusive)
    #startTime    HH:MM                 timed events only (naive wall clock)
    #endTime      HH:MM
    #caldavUID    <uid>                 stable identity; used as the idempotency key
    #tags         cat1,cat2             -> CATEGORIES when exported
    #location     text
    #color        text
    #recurrence   FREQ=...              -> RRULE (no DTSTART, per Trilium)

Times are stored naive in the configured calendar timezone; callers may pass
offset-aware datetimes, which are converted before storage.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import CalendarError

DATE_LABEL = "startDate"
END_DATE_LABEL = "endDate"
START_TIME_LABEL = "startTime"
END_TIME_LABEL = "endTime"
UID_LABEL = "caldavUID"
TAGS_LABEL = "tags"
LOCATION_LABEL = "location"
COLOR_LABEL = "color"
RECURRENCE_LABEL = "recurrence"

#: Labels this server creates, updates and removes. Anything else on the note
#: (template definitions, user labels) is left untouched.
MANAGED_LABELS = frozenset(
    {
        DATE_LABEL,
        END_DATE_LABEL,
        START_TIME_LABEL,
        END_TIME_LABEL,
        UID_LABEL,
        TAGS_LABEL,
        LOCATION_LABEL,
        COLOR_LABEL,
        RECURRENCE_LABEL,
    }
)

_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")


def new_uid() -> str:
    return str(uuid.uuid4())


def stable_uid(calendar_note_id: str, key: str) -> str:
    """A uid derived from a caller-supplied key, so re-runs address the same event.

    ``uuid5`` is deterministic: the same key in the same calendar always yields
    the same uid, so an agent can re-publish a generated list without storing
    uids anywhere and without creating duplicates.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"trilium-calendar:{calendar_note_id}:{key}"))


def parse_time_value(value: str) -> time:
    match = _TIME_RE.match(value.strip())
    if not match:
        raise CalendarError(f"Invalid time {value!r}; expected HH:MM (24h).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise CalendarError(f"Invalid time {value!r}; expected HH:MM (24h).")
    return time(hour, minute)


def parse_date_value(value: str, field_name: str = "date") -> date:
    """Parse an ISO date. A datetime passed to a date field uses its date part."""
    raw = value.strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise CalendarError(
            f"Invalid {field_name} {value!r}; expected an ISO date such as 2026-07-08."
        ) from exc


def parse_datetime_value(
    value: str,
    calendar_tz: ZoneInfo,
    source_tz_name: str | None = None,
    field_name: str = "datetime",
) -> tuple[date, time | None]:
    """Parse an ISO date/datetime into the calendar's timezone.

    Returns ``(date, None)`` for a date-only value (all-day) or ``(date, time)``
    for a datetime. Naive input is *interpreted* in ``source_tz_name`` (or the
    calendar timezone when not given); offset-aware input is taken as-is. Either
    way the result is the wall clock in the calendar's timezone, which is what
    Trilium stores.
    """
    raw = value.strip()
    if not raw:
        raise CalendarError(f"Empty {field_name}.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return parse_date_value(raw, field_name), None

    source = _zone(source_tz_name) if source_tz_name else calendar_tz
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CalendarError(
            f"Invalid {field_name} {value!r}; expected 'YYYY-MM-DD' or an ISO datetime "
            f"such as '2026-07-08T11:00:00' or '2026-07-08T03:00:00Z'."
        ) from exc

    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=source)
    local = aware.astimezone(calendar_tz)
    return local.date(), local.time().replace(second=0, microsecond=0)


def _zone(tz_name: str) -> ZoneInfo:
    from .config import zone_of

    return zone_of(tz_name)


@dataclass
class Event:
    """A calendar event, independent of how Trilium stores it."""

    uid: str
    title: str
    start_date: date
    end_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    description: str = ""
    location: str = ""
    categories: list[str] = field(default_factory=list)
    color: str = ""
    recurrence: str = ""
    note_id: str | None = None
    calendar_name: str | None = None
    calendar_note_id: str | None = None

    @property
    def all_day(self) -> bool:
        return self.start_time is None

    @property
    def last_date(self) -> date:
        """Inclusive last day of the event."""
        return self.end_date or self.start_date

    def validate(self) -> None:
        if not self.title.strip():
            raise CalendarError("Event title must not be empty.")
        if self.end_date and self.end_date < self.start_date:
            raise CalendarError(
                f"end_date {self.end_date.isoformat()} is before start_date {self.start_date.isoformat()}."
            )
        if self.start_time and self.end_time and self.end_date in (None, self.start_date):
            if self.end_time < self.start_time:
                raise CalendarError(
                    f"end_time {self.end_time.strftime('%H:%M')} is before "
                    f"start_time {self.start_time.strftime('%H:%M')} on the same day."
                )

    # ── storage mapping ────────────────────────────────────────────────────

    def labels(self) -> dict[str, str]:
        """The labels to store on the note (empty values are omitted)."""
        labels: dict[str, str] = {DATE_LABEL: self.start_date.isoformat()}
        if self.all_day:
            if self.end_date and self.end_date > self.start_date:
                labels[END_DATE_LABEL] = self.end_date.isoformat()
        else:
            labels[START_TIME_LABEL] = self.start_time.strftime("%H:%M")  # type: ignore[union-attr]
            if self.end_time:
                labels[END_TIME_LABEL] = self.end_time.strftime("%H:%M")
            if self.end_date and self.end_date != self.start_date:
                labels[END_DATE_LABEL] = self.end_date.isoformat()
        if self.uid:
            labels[UID_LABEL] = self.uid
        if self.categories:
            labels[TAGS_LABEL] = ",".join(c.strip() for c in self.categories if c.strip())
        if self.location:
            labels[LOCATION_LABEL] = self.location
        if self.color:
            labels[COLOR_LABEL] = self.color
        if self.recurrence:
            labels[RECURRENCE_LABEL] = self.recurrence
        return {name: value for name, value in labels.items() if value}

    @classmethod
    def from_note(
        cls,
        note: dict,
        content: str = "",
        calendar_name: str | None = None,
        calendar_note_id: str | None = None,
        html_to_text=None,
    ) -> "Event":
        """Build an Event from a Trilium note object (as returned by ETAPI)."""
        labels: dict[str, str] = {}
        for attr in note.get("attributes") or []:
            if attr.get("type") != "label":
                continue
            name = attr.get("name") or ""
            # Skip the template's inherited promoted-label definitions ("label:startDate").
            if name.startswith("label:") or attr.get("isInheritable"):
                continue
            if name in MANAGED_LABELS:
                labels[name] = attr.get("value") or ""

        note_id = note.get("noteId") or ""
        start_raw = labels.get(DATE_LABEL)
        if not start_raw:
            raise CalendarError(f"Note {note_id} is not an event (no #{DATE_LABEL} label).")

        start_date = parse_date_value(start_raw, "startDate")
        start_time = parse_time_value(labels[START_TIME_LABEL]) if labels.get(START_TIME_LABEL) else None
        end_time = parse_time_value(labels[END_TIME_LABEL]) if labels.get(END_TIME_LABEL) else None
        end_date = parse_date_value(labels[END_DATE_LABEL], "endDate") if labels.get(END_DATE_LABEL) else None
        if end_date is None and not start_time:
            end_date = start_date  # single all-day event

        text = html_to_text(content) if html_to_text else (content or "")
        return cls(
            uid=labels.get(UID_LABEL) or f"{note_id}@trilium",
            title=note.get("title") or "",
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            description=text,
            location=labels.get(LOCATION_LABEL, ""),
            categories=[c for c in (labels.get(TAGS_LABEL) or "").split(",") if c.strip()],
            color=labels.get(COLOR_LABEL, ""),
            recurrence=labels.get(RECURRENCE_LABEL, ""),
            note_id=note_id,
            calendar_name=calendar_name,
            calendar_note_id=calendar_note_id,
        )

    # ── ranges and presentation ───────────────────────────────────────────

    def start_datetime(self, tz: ZoneInfo) -> datetime:
        return datetime.combine(self.start_date, self.start_time or time.min, tzinfo=tz)

    def end_datetime(self, tz: ZoneInfo) -> datetime:
        if self.all_day:
            return datetime.combine(self.last_date, time.max, tzinfo=tz)
        return datetime.combine(self.end_date or self.start_date, self.end_time or self.start_time, tzinfo=tz)

    def overlaps(self, range_start: date, range_end: date) -> bool:
        return self.start_date <= range_end and self.last_date >= range_start

    def to_result(self, tz: ZoneInfo) -> dict:
        """JSON-friendly shape returned to the agent."""
        if self.all_day:
            start = self.start_date.isoformat()
            end = self.last_date.isoformat()
        else:
            start = self.start_datetime(tz).isoformat()
            end = self.end_datetime(tz).isoformat() if (self.end_time or self.end_date) else None
        return {
            "uid": self.uid,
            "calendar_name": self.calendar_name,
            "calendar_note_id": self.calendar_note_id,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "categories": self.categories,
            "all_day": self.all_day,
            "start": start,
            "end": end,
            "start_date": self.start_date.isoformat(),
            "start_time": self.start_time.strftime("%H:%M") if self.start_time else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "end_time": self.end_time.strftime("%H:%M") if self.end_time else None,
            "recurrence_rule": self.recurrence,
            "color": self.color,
            "timezone": str(tz),
            "note_id": self.note_id,
        }


def shift_days(value: date, days: int) -> date:
    return value + timedelta(days=days)

"""Runtime configuration, read from the environment.

Calendars are configured as a comma-separated list of Trilium note ids, each
optionally given a human name:

    TRILIUM_CALENDARS=0hq1wCTuDBjA:Gaming,kf8Xq2vLpZ1a:Personal

The name is what the agent passes as ``calendar_name`` (and what
``calendar_list_calendars`` returns). Without a name, the calendar note's own
title in Trilium is used. The first calendar is the default for tools that
allow omitting ``calendar_name``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import CalendarError

DEFAULT_TIMEZONE = "Asia/Shanghai"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class CalendarSpec:
    """One Trilium note treated as a calendar."""

    note_id: str
    alias: str | None = None
    default: bool = False


@dataclass
class Settings:
    url: str
    token: str
    calendars: list[CalendarSpec] = field(default_factory=list)
    timezone: str = DEFAULT_TIMEZONE
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8102
    path: str = "/mcp"
    verify_ssl: bool = True
    ca_bundle: str | None = None
    timeout: float = 30.0
    search_limit: int = 5000
    log_level: str = "INFO"
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return zone_of(self.timezone)

    def spec_for(self, note_id: str) -> CalendarSpec | None:
        for spec in self.calendars:
            if spec.note_id == note_id:
                return spec
        return None


def zone_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env dependent
        raise CalendarError(
            f"Unknown timezone {name!r}. Use an IANA name such as 'Asia/Shanghai' or 'UTC'."
        ) from exc


def parse_calendars(raw: str) -> list[CalendarSpec]:
    """Parse ``id[:name]`` entries separated by commas, semicolons or newlines.

    A repeated note id is listed once; if an earlier entry has no name and a
    later one provides it, the name is taken from the later entry.
    """
    specs: list[CalendarSpec] = []
    by_id: dict[str, int] = {}
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        alias: str | None = None
        for sep in (":", "="):
            if sep in entry:
                note_id, alias = entry.split(sep, 1)
                note_id, alias = note_id.strip(), alias.strip() or None
                break
        else:
            note_id = entry
        if not note_id:
            continue
        if note_id in by_id:
            index = by_id[note_id]
            existing = specs[index]
            if existing.alias is None and alias is not None:
                specs[index] = CalendarSpec(
                    note_id=note_id, alias=alias, default=existing.default
                )
            continue
        by_id[note_id] = len(specs)
        specs.append(CalendarSpec(note_id=note_id, alias=alias, default=not specs))
    return specs


def load_settings(require_calendars: bool = True) -> Settings:
    url = _env("TRILIUM_URL", "TRILIUM_BASE_URL")
    token = _env("TRILIUM_ETAPI_TOKEN", "TRILIUM_TOKEN", "TRILIUM_API_TOKEN")
    raw_calendars = _env("TRILIUM_CALENDARS", "TRILIUM_CALENDAR")

    missing = []
    if not url:
        missing.append("TRILIUM_URL")
    if not token:
        missing.append("TRILIUM_ETAPI_TOKEN")
    if require_calendars and not raw_calendars:
        missing.append("TRILIUM_CALENDARS")
    if missing:
        raise CalendarError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". See .env.example for the full list."
        )

    transport = _env("MCP_TRANSPORT", default="stdio").lower()
    if transport in ("http", "streamable_http"):
        transport = "streamable-http"
    if transport not in ("stdio", "streamable-http", "sse"):
        raise CalendarError(
            f"MCP_TRANSPORT must be 'stdio', 'streamable-http' or 'sse', got {transport!r}."
        )

    try:
        port = int(_env("MCP_PORT", default="8102"))
    except ValueError as exc:
        raise CalendarError(f"MCP_PORT must be an integer, got {os.environ.get('MCP_PORT')!r}.") from exc

    return Settings(
        url=url.rstrip("/"),
        token=token,
        calendars=parse_calendars(raw_calendars),
        timezone=_env("TRILIUM_TIMEZONE", default=DEFAULT_TIMEZONE),
        transport=transport,
        host=_env("MCP_HOST", default="0.0.0.0"),
        port=port,
        path=_env("MCP_PATH", default="/mcp") or "/mcp",
        verify_ssl=_env_bool("TRILIUM_VERIFY_SSL", True),
        ca_bundle=_env("TRILIUM_CA_BUNDLE") or None,
        timeout=float(_env("TRILIUM_TIMEOUT", default="30")),
        search_limit=int(_env("TRILIUM_SEARCH_LIMIT", default="5000")),
        log_level=_env("LOG_LEVEL", default="INFO").upper(),
        allowed_hosts=[
            h.strip()
            for h in _env("MCP_ALLOWED_HOSTS").replace(";", ",").split(",")
            if h.strip()
        ],
        allowed_origins=[
            o.strip()
            for o in _env("MCP_ALLOWED_ORIGINS").replace(";", ",").split(",")
            if o.strip()
        ],
    )

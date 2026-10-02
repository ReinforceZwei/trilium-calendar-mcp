# trilium-calendar-mcp

Calendar **events** as MCP tools for AI agents, stored as notes in [Trilium](https://trilium.rocks/).

It is a drop-in replacement for the Nextcloud Calendar MCP (`nc_calendar_*`): the tool names and
arguments are deliberately the same shape (`calendar_*`), so an agent that already knows how to
write calendar events only needs the tool prefix changed — no relearning, no new prompt structure.

```
AI agent  ──MCP──▶  trilium-calendar-mcp  ──ETAPI──▶  Trilium notes
                                                         │
                                      CalDAV facade ─────┘──▶ Apple Calendar / DAVx⁵ / Thunderbird
```

Notes carrying `#startDate` are calendar events in Trilium's own calendar view, and the same notes
can be served over CalDAV by a facade (see *Interoperability*), so one set of notes feeds the agent,
the Trilium UI and standard calendar clients.

## Why not let the agent drive the Trilium MCP directly?

The generic Trilium MCP has no calendar semantics: creating one event means `create_note` plus one
`set_attribute` call per label, the agent has to know every label name, the inclusive-`#endDate`
convention and the timezone rules, and mistakes fail **silently** — a note labelled `#startdate`
simply never appears in the calendar, with no error. Those rules live in code here, and every event
is validated on the way in.

## Quickstart

```bash
docker run --rm -i \
  -e TRILIUM_URL=https://trilium.example.com \
  -e TRILIUM_ETAPI_TOKEN=your-etapi-token \
  -e TRILIUM_CALENDARS=0hq1wCTuDBjA:Gaming \
  ghcr.io/reinforcezwei/trilium-calendar-mcp:latest
```

That runs over **stdio**. For an HTTP (streamable) endpoint — what a remote agent infrastructure
usually needs — add:

```bash
docker run --rm -p 127.0.0.1:8102:8102 \
  -e MCP_TRANSPORT=streamable-http -e MCP_HOST=0.0.0.0 -e MCP_PORT=8102 \
  -e TRILIUM_URL=https://trilium.example.com \
  -e TRILIUM_ETAPI_TOKEN=your-etapi-token \
  -e TRILIUM_CALENDARS=0hq1wCTuDBjA:Gaming \
  ghcr.io/reinforcezwei/trilium-calendar-mcp:latest
# MCP endpoint: http://host:8102/mcp
```

`docker-compose.yml` wires the same thing up with an `.env` file.

> **HTTP endpoints and `Host` headers.** The MCP SDK rejects requests whose `Host` header it does
> not recognise (DNS-rebinding protection) with `421 Invalid Host header` — which is what happens
> when the container is reached under a service name (`http://trilium-calendar-mcp:8102`) or through
> a proxy. List those names in `MCP_ALLOWED_HOSTS` (`host:port`, comma-separated):
>
> ```bash
> -e MCP_ALLOWED_HOSTS=trilium-calendar-mcp:8102,cal-mcp.internal:443
> ```
>
> Not needed for `stdio`, and not needed when the client connects to `127.0.0.1:<port>` directly.

### Client configuration (stdio)

```json
{
  "mcpServers": {
    "trilium-calendar": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "--env-file", "/path/.env",
               "ghcr.io/reinforcezwei/trilium-calendar-mcp:latest"]
    }
  }
}
```

## Configuration

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TRILIUM_URL` | yes | — | Trilium base URL (no `/etapi` suffix) |
| `TRILIUM_ETAPI_TOKEN` | yes | — | ETAPI token (Trilium → Options → ETAPI) |
| `TRILIUM_CALENDARS` | yes | — | Calendars to expose — see below |
| `TRILIUM_TIMEZONE` | no | `Asia/Shanghai` | Timezone used to interpret naive datetimes |
| `MCP_TRANSPORT` | no | `stdio` | `stdio`, `streamable-http` (alias `http`) or `sse` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | no | `0.0.0.0` / `8102` / `/mcp` | HTTP binding |
| `MCP_ALLOWED_HOSTS` | no | — | `host:port` names the HTTP endpoint accepts (see the note above) |
| `TRILIUM_VERIFY_SSL` | no | `true` | Set `false` for a self-signed instance |
| `TRILIUM_CA_BUNDLE` | no | — | Custom CA bundle path |
| `TRILIUM_TIMEOUT` | no | `30` | Per-request timeout (seconds) |
| `TRILIUM_SEARCH_LIMIT` | no | `5000` | Max notes fetched per calendar scan |
| `LOG_LEVEL` | no | `INFO` | Log verbosity (logs go to stderr) |

### Calendars and their names

`TRILIUM_CALENDARS` is a comma-separated list of Trilium **note ids**, each optionally named:

```bash
TRILIUM_CALENDARS=0hq1wCTuDBjA:Gaming,kf8Xq2vLpZ1a:Personal
# or, without aliases (the note's own title is then used as the name)
TRILIUM_CALENDARS=0hq1wCTuDBjA,kf8Xq2vLpZ1a
```

* The **name after the colon** (or `=`) is the `calendar_name` the agent uses. Without an alias the
  calendar note's Trilium **title** is used instead, so "Gaming" works either way once the note is
  titled that.
* `calendar_list_calendars` returns every calendar with `name`, `note_id`, `title`, `timezone`,
  `color` and `event_count` — that is how the agent resolves "put it in Gaming".
* All tools accept `calendar_name`; it matches the name/alias, the note title, or the note id,
  case-insensitively. The first configured calendar is the default when `calendar_name` is omitted.
* An unknown name is an error that lists the valid ones, so the agent can self-correct.

## Tools

| Tool | Purpose |
| --- | --- |
| `calendar_list_calendars` | List calendars with their names/ids/timezones — call this first |
| `calendar_list_events` | Events in a date range; filters on title, categories, location |
| `calendar_get_event` | One event by uid, including description |
| `calendar_create_event` | Create an event (returns its uid) |
| `calendar_update_event` | Patch an event; only the arguments you pass change |
| `calendar_delete_event` | Delete by uid (unknown uid = success, safe retries) |
| `calendar_get_upcoming_events` | Events starting in the next N days |
| `calendar_upsert_events` | Bulk create-or-update, keyed on uid |
| `calendar_delete_events` | Bulk delete by uid |

### Migrating an agent from the Nextcloud MCP

Rename the tool prefix; arguments keep their names:

| Nextcloud MCP | this server |
| --- | --- |
| `nc_calendar_list_calendars` | `calendar_list_calendars` |
| `nc_calendar_list_events` | `calendar_list_events` |
| `nc_calendar_get_event` | `calendar_get_event` |
| `nc_calendar_create_event` | `calendar_create_event` |
| `nc_calendar_update_event` | `calendar_update_event` |
| `nc_calendar_delete_event` | `calendar_delete_event` |
| `nc_calendar_get_upcoming_events` | `calendar_get_upcoming_events` |
| `nc_calendar_bulk_operations` | `calendar_upsert_events` / `calendar_delete_events` |

Point the prompt's calendar name at a configured name (e.g. `Gaming`) and the switch is done.
`calendar_create_event` also accepts `event_uid`: pass an existing uid to **update** instead of
creating a duplicate, which makes unattended re-runs idempotent.

## Semantics the agent can rely on

* **Dates** — `start_datetime` takes `YYYY-MM-DD` (all-day) or an ISO datetime. Naive datetimes are
  read in the `timezone` argument, else the calendar's timezone; `Z`/offset values are converted.
  Either way, what is stored is the wall clock in the calendar's timezone.
* **All-day ranges** — `end_datetime` is the **last day of the event, inclusive** ("until the 28th"
  means the 28th). This differs from Nextcloud's `DTEND`, which is the day *after*: a Nextcloud
  all-day event ending `2026-07-28` covered 7/8–7/27. Re-check any all-day ranges in the old prompt
  when you migrate.
* **Times** — timed events get `#startTime`/`#endTime`; an event with no end time is open-ended.
* **Identity** — every event carries a stable `#caldavUID`. New events get a UUID; pass `event_uid`
  (or use `calendar_upsert_events`) to update in place.
* **Descriptions** — plain text, line breaks preserved. Markdown syntax is not interpreted.
* **Not stored** — `status`, `priority`, `privacy`, `attendees`, `url`, reminders/alarms. These
  arguments are *accepted* so an existing prompt does not break, and echoed back in `ignored_fields`
  so the agent can see they had no effect.
* **Recurrence** — pass an RRULE in `recurrence_rule` (`FREQ=WEEKLY;BYDAY=MO`), optionally bounded
  with `recurrence_end_date`; `recurring=False` or `recurrence_rule=""` removes it.

## How events are stored

| Trilium label | Meaning |
| --- | --- |
| `#startDate` | `YYYY-MM-DD`, always present (this is what makes it an event) |
| `#endDate` | last day, **inclusive** |
| `#startTime` / `#endTime` | `HH:MM`, timed events only, naive wall clock |
| `#caldavUID` | stable identity / idempotency key |
| `#tags` | categories, comma-separated |
| `#location`, `#color`, `#recurrence` | passthrough fields |

Notes are created as normal Trilium text notes with the event description as their body, so they
read naturally in the note tree and the calendar view.

## Interoperability with a CalDAV facade

The labels above match the mapping used by the Trilium CalDAV facade
(`#endDate` inclusive → `DTEND` +1 day, `#caldavUID` → `UID`, `#tags` → `CATEGORIES`), so events
written by an agent are served correctly to Apple Calendar, DAVx⁵ and Thunderbird by the same
facade. Nothing here requires the facade, and the facade does not require this server.

## Limitations

* **No reminders/alarms, attendees, or event status** — Trilium notes have no equivalent.
* **No todos/VTODO** — event notes only (Trilium's `#todoDate` task notes are a follow-up).
* **Timezones are per calendar, not per event** — a single calendar has one timezone; events from
  several zones are normalised into it.
* **Listing scans the calendar's children** (one ETAPI search) and filters locally; fine for the
  thousands-of-events scale, not for millions.
* **No ETags/conflict detection** — last write wins, which is fine for an agent-owned calendar.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest tests/test_event.py            # unit tests, no server needed
TRILIUM_URL=https://... TRILIUM_ETAPI_TOKEN=... pytest   # + integration tests against a live instance
```

Integration tests create a temporary calendar note under `root`, exercise create/list/update/
upsert/delete against it, and delete it afterwards (cascading to the events they made). No existing
notes are touched.

### Releasing

```bash
./release.sh 0.2.0          # bump version, commit, tag
./release.sh 0.2.0 --push   # ... and push the commit + tag
```

Pushing a `v*` tag runs the test suite, then builds and publishes
`ghcr.io/<owner>/trilium-calendar-mcp` (`0.2.0`, `0.2`, `latest`) for `linux/amd64` and
`linux/arm64`, and opens a GitHub release.

## License

MIT

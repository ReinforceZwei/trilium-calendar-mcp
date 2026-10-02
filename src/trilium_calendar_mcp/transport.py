"""DNS-rebinding (Host/Origin) protection policy for the HTTP transport.

The MCP SDK answers `421 Invalid Host header` for requests whose `Host` is not
allow-listed, and its own default is load-order dependent: protection is
auto-enabled for loopback binds, but a container bind (`0.0.0.0`) silently ends
up in a different state depending on SDK version (early versions produced an
*empty* allowlist and rejected every hostname — see
modelcontextprotocol/python-sdk#1798, a problem that cost plenty of people a
day each).

Rather than inherit whatever the installed SDK does, the policy is decided here
and passed down explicitly:

* `MCP_ALLOWED_HOSTS` set  -> protection on, exactly those host values allowed.
* bound to loopback        -> protection on, localhost names allowed (this is the
  case it protects against: a browser on the same machine reaching the port).
* bound anywhere else      -> protection off. A container or a proxy terminates
  requests under names this server cannot guess, and DNS rebinding is not the
  threat model there (network isolation and auth are).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Host values accepted when the server is bound to a loopback address. The
#: `:*` forms are the SDK's port wildcard; the bare forms cover clients that
#: omit the port.
LOOPBACK_HOSTS: tuple[str, ...] = (
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "[::1]",
    "[::1]:*",
)

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0:0:0:0:0:0:0:1"})


@dataclass(frozen=True)
class TransportSecurity:
    """What to pass to `server.run(transport_security=...)`."""

    enable_dns_rebinding_protection: bool
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    reason: str = ""


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK_NAMES


def origins_for(hosts: list[str]) -> list[str]:
    """Origins matching the allowed hosts (both schemes), for browser clients."""
    return [f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")]


def resolve_transport_security(
    host: str, allowed_hosts: list[str] | None = None, allowed_origins: list[str] | None = None
) -> TransportSecurity:
    hosts = [h.strip() for h in (allowed_hosts or []) if h and h.strip()]
    origins = [o.strip() for o in (allowed_origins or []) if o and o.strip()]

    if hosts:
        return TransportSecurity(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins or origins_for(hosts),
            reason=f"enabled by MCP_ALLOWED_HOSTS ({len(hosts)} host value(s))",
        )

    if is_loopback(host):
        return TransportSecurity(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(LOOPBACK_HOSTS),
            allowed_origins=origins_for(list(LOOPBACK_HOSTS)),
            reason=f"bound to loopback ({host}); localhost names allowed",
        )

    return TransportSecurity(
        enable_dns_rebinding_protection=False,
        reason=f"bound to {host or 'all interfaces'}; set MCP_ALLOWED_HOSTS to enable the check",
    )

"""Unit tests for the HTTP DNS-rebinding policy (no server needed)."""

from __future__ import annotations

import pytest

from trilium_calendar_mcp.transport import (
    LOOPBACK_HOSTS,
    is_loopback,
    origins_for,
    resolve_transport_security,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST", " 127.0.0.1 "])
def test_loopback_detection(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "trilium-calendar-mcp", ""])
def test_non_loopback_detection(host):
    assert not is_loopback(host)


def test_container_bind_needs_no_configuration():
    """The default docker-compose case: bind all interfaces, no allowlist."""
    policy = resolve_transport_security("0.0.0.0")
    assert policy.enable_dns_rebinding_protection is False
    assert policy.allowed_hosts == []
    assert "MCP_ALLOWED_HOSTS" in policy.reason


def test_loopback_bind_keeps_protection_with_localhost_names():
    policy = resolve_transport_security("127.0.0.1")
    assert policy.enable_dns_rebinding_protection is True
    assert "localhost:*" in policy.allowed_hosts
    assert "127.0.0.1:*" in policy.allowed_hosts
    # bare names too, for clients that omit the port
    assert "localhost" in policy.allowed_hosts
    assert policy.allowed_hosts == list(LOOPBACK_HOSTS)


def test_explicit_allowlist_enables_protection_for_those_hosts():
    policy = resolve_transport_security("127.0.0.1", ["cal.internal:443", "svc:8102"])
    assert policy.enable_dns_rebinding_protection is True
    assert policy.allowed_hosts == ["cal.internal:443", "svc:8102"]
    assert "MCP_ALLOWED_HOSTS" in policy.reason


def test_explicit_allowlist_also_works_for_container_binds():
    policy = resolve_transport_security("0.0.0.0", ["cal.internal:443"])
    assert policy.enable_dns_rebinding_protection is True
    assert policy.allowed_hosts == ["cal.internal:443"]


def test_origins_are_derived_from_hosts():
    policy = resolve_transport_security("0.0.0.0", ["cal.internal:443"])
    assert policy.allowed_origins == ["http://cal.internal:443", "https://cal.internal:443"]


def test_explicit_origins_override_the_derived_ones():
    policy = resolve_transport_security("0.0.0.0", ["cal.internal:443"], ["https://ui.example"])
    assert policy.allowed_origins == ["https://ui.example"]


def test_blank_entries_are_ignored():
    policy = resolve_transport_security("0.0.0.0", ["", "  ", "cal.internal:443"])
    assert policy.allowed_hosts == ["cal.internal:443"]


def test_origins_for_covers_both_schemes():
    assert origins_for(["host:8102"]) == ["http://host:8102", "https://host:8102"]

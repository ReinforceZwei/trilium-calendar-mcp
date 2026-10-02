"""Errors surfaced to the calling agent.

``CalendarError`` is raised for anything the caller can fix (bad date, unknown
calendar, unknown uid) — the message is written to be read by a model, so it
always names the valid alternatives.
"""

from __future__ import annotations


class CalendarError(Exception):
    """Bad input or impossible request. Message is agent-facing."""


class TriliumError(Exception):
    """The Trilium backend refused a call or returned something unusable."""

"""Canonical UTC timestamp helpers shared by the watchdog and remote modules.

Every persisted or transmitted timestamp in this project uses the same
second-resolution UTC format. Keeping the format in one place means a change
can no longer silently break freshness checks that parse the same strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_datetime() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def utc_now() -> str:
    """Return the current time formatted for persistence and the API."""

    return now_datetime().strftime(TIMESTAMP_FORMAT)


def format_utc(value: datetime) -> str:
    """Format a datetime using the project-wide UTC timestamp format."""

    return value.strftime(TIMESTAMP_FORMAT)


def parse_utc(value: str) -> datetime:
    """Parse a project timestamp into a timezone-aware UTC datetime."""

    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def shift_utc(value: str, *, minutes: int = 0, seconds: int = 0) -> str:
    """Return ``value`` shifted by the given offset, in the same format."""

    shifted = parse_utc(value) + timedelta(minutes=minutes, seconds=seconds)
    return format_utc(shifted)

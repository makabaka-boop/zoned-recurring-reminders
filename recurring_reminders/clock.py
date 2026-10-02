"""Controllable clocks.

The service never reads the wall clock directly; it asks an injected
Clock.  Tests and the HTTP API drive a ManualClock so "clock advancement"
is deterministic and repeatable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class SystemClock:
    """Real wall clock (aware UTC)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Deterministic clock advanced explicitly by the caller."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("ManualClock.set requires a timezone-aware datetime")
        self._now = when.astimezone(UTC)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

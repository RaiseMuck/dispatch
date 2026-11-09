"""Time source. Nothing in Dispatch calls datetime.now() directly - it asks a Clock.

SystemClock is the real wall clock; FakeClock is a hand-cranked clock used by
tests and by --dry-run so a whole day can be simulated in a second.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware datetime in US Eastern."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC).astimezone(ET)


class FakeClock:
    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start time")
        self._t = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._t.astimezone(ET)

    def advance(self, delta: timedelta) -> datetime:
        # Arithmetic in UTC so DST changes never produce a skipped/doubled minute.
        self._t = self._t + delta
        return self.now()

    def set(self, when: datetime) -> None:
        self._t = when.astimezone(UTC)


def to_et(dt: datetime) -> datetime:
    return dt.astimezone(ET)


def fmt_et(dt: datetime | None) -> str:
    """Short human time, e.g. 'Fri 10/3 11:00 AM'."""
    if dt is None:
        return "no due date"
    d = dt.astimezone(ET)
    hour12 = d.hour % 12 or 12
    ampm = "AM" if d.hour < 12 else "PM"
    return f"{d:%a} {d.month}/{d.day} {hour12}:{d.minute:02d} {ampm}"

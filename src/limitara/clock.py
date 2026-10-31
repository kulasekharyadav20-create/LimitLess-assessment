"""One shared notion of "now" for every process.

Instants are timezone-aware and travel as UTC ISO-8601 strings.
Business dates (delivery, validity, "confirm by Friday") are dates in IST.
Set LIMITARA_FIXED_NOW (e.g. 2026-10-31T05:30:00+00:00) to pin every process
to the same moment; leave it unset for real time.
"""

import os
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FIXED_NOW_ENV = "LIMITARA_FIXED_NOW"


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, instant: datetime):
        self._instant = _require_aware(instant).astimezone(UTC)

    def now(self) -> datetime:
        return self._instant


def _require_aware(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must carry a timezone; naive datetimes are ambiguous")
    return instant


def parse_instant(text: str) -> datetime:
    """Parse an ISO-8601 instant. A missing offset is refused, never guessed."""
    return _require_aware(datetime.fromisoformat(text)).astimezone(UTC)


def to_wire(instant: datetime) -> str:
    return _require_aware(instant).astimezone(UTC).isoformat()


def ist_date(instant: datetime) -> date:
    """The business date in India at this instant."""
    return _require_aware(instant).astimezone(IST).date()


def clock_from_env(environ: dict[str, str] | None = None) -> Clock:
    env = os.environ if environ is None else environ
    pinned = env.get(FIXED_NOW_ENV)
    return FixedClock(parse_instant(pinned)) if pinned else SystemClock()

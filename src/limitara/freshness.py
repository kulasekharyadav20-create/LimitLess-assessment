"""How old is too old. Every read is stamped; stale reads are flagged, not used silently.

The limits are judgment calls, kept in one place so they can be defended and changed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_AGE: dict[str, timedelta] = {
    "stock": timedelta(minutes=15),
    "capacity": timedelta(minutes=15),
    "credit_status": timedelta(minutes=15),
    "price_list": timedelta(hours=24),
    "customer_terms": timedelta(hours=24),
    "dispatch_schedule": timedelta(hours=24),
}

# A read stamped slightly in the future is clock skew; far in the future is suspect.
FUTURE_TOLERANCE = timedelta(seconds=60)


@dataclass(frozen=True)
class Freshness:
    source: str
    fresh: bool
    age: timedelta
    reason: str | None = None


def check(source: str, read_at: datetime, now: datetime) -> Freshness:
    """Judge one read. An unknown source raises: we never invent a default limit."""
    limit = MAX_AGE[source]
    age = now - read_at
    if age < -FUTURE_TOLERANCE:
        return Freshness(source, False, age, f"{source} read is stamped in the future")
    if age > limit:
        return Freshness(source, False, age, f"{source} read is {age} old, limit is {limit}")
    return Freshness(source, True, age)

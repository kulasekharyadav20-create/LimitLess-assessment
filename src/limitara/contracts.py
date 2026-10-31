"""Types and errors shared by Core and its clients."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Stamped:
    """Data plus where it came from and when it was read (principle 2)."""

    source: str
    read_at: datetime
    data: Any


class Unknown(Exception):
    """We could not get a trustworthy answer. Never to be treated as zero."""

    def __init__(self, source: str, why: str):
        super().__init__(f"{source}: {why}")
        self.source, self.why = source, why


class Unavailable(Unknown):
    """A stand-in could not be reached or timed out."""


class Rejected(Exception):
    """A stand-in refused a write for a business reason (not retried)."""

    def __init__(self, source: str, code: str, detail: str = ""):
        super().__init__(f"{source} refused: {code} {detail}")
        self.source, self.code, self.detail = source, code, detail


class Conflict(Exception):
    """Idempotency conflict: a bug in the caller, never retried."""

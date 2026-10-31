"""Every judgment-call number in one place, so each can be defended and changed live."""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal


@dataclass(frozen=True)
class Config:
    horizon_days: int = 30                   # planning window; capacity rows exist for this many days
    production_start_lag_days: int = 1       # earliest production day is tomorrow
    handling_days: int = 1                   # receiving and packing before goods can ship
    default_floor_pct: Decimal = Decimal("12")
    count_old_days: int = 7                  # stock counted longer ago than this raises COUNT_OLD
    validity_cutoff_ist: time = time(18, 0)  # "confirm by Friday" means Friday 18:00 IST
    history_days: int = 7                    # mail older than this is history, not new work
    slip_check_seconds: int = 60
    escalate_hours: int = 4
    near_duplicate_hours: int = 24
    read_timeout: float = 2.0
    write_timeout: float = 5.0
    intent_backoff_seconds: tuple[int, ...] = (5, 15, 60, 300)
    intent_max_attempts: int = 8
    freshness_retry: bool = True


DEFAULT = Config()

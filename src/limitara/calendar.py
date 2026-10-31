"""Shipping calendar rules shared by the stand-ins and the promise engine.

Kept tiny and pure. A ship date must be a carrier weekday and not a holiday.
A delivery that would land on a Sunday or a holiday moves to the next day that is neither.
"""

from datetime import date, timedelta

SEARCH_LIMIT_DAYS = 90


def is_carrier_day(day: date, weekdays: frozenset[int] | set[int], holidays: frozenset[date] | set[date]) -> bool:
    return day.weekday() in weekdays and day not in holidays


def next_ship_date(earliest: date, weekdays, holidays) -> date | None:
    day = earliest
    for _ in range(SEARCH_LIMIT_DAYS):
        if is_carrier_day(day, weekdays, holidays):
            return day
        day += timedelta(days=1)
    return None


def delivery_date(ship: date, transit_days: int, holidays) -> date:
    day = ship + timedelta(days=transit_days)
    while day.weekday() == 6 or day in holidays:
        day += timedelta(days=1)
    return day


def parse_weekdays(text: str) -> frozenset[int]:
    return frozenset(int(p) for p in text.split(",") if p != "")

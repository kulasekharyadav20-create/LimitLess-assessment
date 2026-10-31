from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from limitara.clock import (
    FIXED_NOW_ENV,
    FixedClock,
    SystemClock,
    clock_from_env,
    ist_date,
    parse_instant,
    to_wire,
)


def test_fixed_clock_always_returns_the_same_instant():
    instant = datetime(2026, 10, 31, 5, 30, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == clock.now() == instant


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 10, 31, 5, 30))
    with pytest.raises(ValueError):
        parse_instant("2026-10-31T05:30:00")


def test_late_utc_evening_is_already_tomorrow_in_india():
    # 20:00 UTC on the 30th is 01:30 IST on the 31st
    assert ist_date(datetime(2026, 10, 30, 20, 0, tzinfo=UTC)) == date(2026, 10, 31)
    # 18:29 UTC is 23:59 IST, still the 30th
    assert ist_date(datetime(2026, 10, 30, 18, 29, tzinfo=UTC)) == date(2026, 10, 30)


def test_other_offsets_are_normalised_to_utc_on_the_wire():
    plus_two = timezone(timedelta(hours=2))
    assert to_wire(datetime(2026, 10, 31, 7, 0, tzinfo=plus_two)) == "2026-10-31T05:00:00+00:00"


def test_env_pins_the_clock_for_every_process():
    clock = clock_from_env({FIXED_NOW_ENV: "2026-10-31T05:30:00+00:00"})
    assert clock.now() == datetime(2026, 10, 31, 5, 30, tzinfo=UTC)


def test_without_env_the_clock_is_real_time():
    assert isinstance(clock_from_env({}), SystemClock)

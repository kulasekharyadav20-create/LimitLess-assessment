from datetime import UTC, datetime, timedelta

import pytest

from limitara.freshness import check

NOW = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)


def test_recent_stock_read_is_fresh():
    assert check("stock", NOW - timedelta(minutes=14, seconds=59), NOW).fresh


def test_exactly_at_the_limit_is_still_fresh():
    assert check("stock", NOW - timedelta(minutes=15), NOW).fresh


def test_one_second_past_the_limit_is_stale_with_a_reason():
    result = check("stock", NOW - timedelta(minutes=15, seconds=1), NOW)
    assert not result.fresh
    assert "stock" in result.reason


def test_slow_changing_sources_get_a_longer_limit():
    six_hours_old = NOW - timedelta(hours=6)
    assert check("price_list", six_hours_old, NOW).fresh
    assert not check("capacity", six_hours_old, NOW).fresh


def test_read_stamped_in_the_future_is_not_trusted():
    assert not check("capacity", NOW + timedelta(minutes=10), NOW).fresh


def test_small_clock_skew_is_tolerated():
    assert check("capacity", NOW + timedelta(seconds=30), NOW).fresh


def test_unknown_source_raises_instead_of_inventing_a_limit():
    with pytest.raises(KeyError):
        check("weather", NOW, NOW)

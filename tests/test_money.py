from decimal import Decimal as D

import pytest

from limitara.money import (
    MoneyError,
    apply_discount,
    floor_price,
    line_total,
    margin_pct,
    meets_floor,
    to_money,
    to_wire,
)


def test_float_is_refused_not_guessed():
    with pytest.raises(MoneyError):
        to_money(0.1)


def test_garbage_and_non_finite_amounts_are_refused():
    for bad in ("abc", "NaN", "Infinity", None):
        with pytest.raises(MoneyError):
            to_money(bad)


def test_decimal_is_exact_where_float_is_not():
    assert to_money("0.1") + to_money("0.2") == to_money("0.3")


def test_discount_rounds_half_up():
    assert apply_discount(D("1187.50"), D("5")) == D("1128.13")  # 1128.125


def test_discount_bounds():
    for bad in (D("-1"), D("100"), D("150")):
        with pytest.raises(MoneyError):
            apply_discount(D("100"), bad)


def test_margin_is_on_price_not_cost():
    assert margin_pct(D("1250"), D("1000")) == D("20")


def test_floor_price_rounds_up_so_it_really_passes():
    # exact lowest price is 1136.3636...; half-up would give 1136.36
    assert floor_price(D("1000"), D("12")) == D("1136.37")
    assert meets_floor(D("1136.37"), D("1000"), D("12"))
    assert not meets_floor(D("1136.36"), D("1000"), D("12"))


def test_margin_exactly_at_floor_passes():
    assert meets_floor(D("1250"), D("1000"), D("20"))


def test_one_paisa_under_floor_fails():
    # floor price for cost 1000 at 20% is exactly 1250.00
    assert not meets_floor(D("1249.99"), D("1000"), D("20"))


def test_selling_below_cost_fails_floor():
    assert not meets_floor(D("900"), D("1000"), D("12"))


def test_line_total_is_rounded_unit_price_times_quantity():
    # 333.33 * 0.967 = 322.33011 -> 322.33 ; 322.33 * 300 = 96699.00
    unit = apply_discount(D("333.33"), D("3.3"))
    assert unit == D("322.33")
    assert line_total(unit, 300) == D("96699.00")


def test_quantity_must_be_a_positive_integer():
    for bad in (0, -5, 2.5, True):
        with pytest.raises(MoneyError):
            line_total(D("10"), bad)


def test_wire_format_is_a_two_decimal_string():
    assert to_wire(D("1156.2")) == "1156.20"
    assert isinstance(to_wire(D("1")), str)

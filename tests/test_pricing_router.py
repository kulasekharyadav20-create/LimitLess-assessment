from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from limitara.core.approvals.router import route_approvals, can_satisfy
from limitara.core.config import DEFAULT
from limitara.core.pricing.engine import PriceListInfo, PricingInputs, TermsInfo, credit_flags, price_request
from limitara.core.pricing.flags import Flag

TODAY = date(2026, 10, 31)
FRIDAY = datetime(2026, 11, 6, 12, 30, tzinfo=UTC)  # Friday 18:00 IST
COST = {"FL-50": D("1000.00"), "FL-80": D("1700.00")}
STD = PriceListInfo("PL-STD-Q4", None, TODAY - timedelta(30), TODAY + timedelta(60), {"FL-50": D("1250.00"), "FL-80": D("2100.00")})
OLD = PriceListInfo("PL-STD-Q3", None, TODAY - timedelta(122), TODAY - timedelta(31), {"FL-50": D("1180.00"), "FL-80": D("1980.00")})
TERMS5 = TermsInfo("TM-1", D("5.00"), TODAY - timedelta(200), TODAY + timedelta(150), "ask", None)
AUTH = {"sales_manager": D("9.00"), "sales_head": D("5.00")}


def inp(lines=(("FL-50", 100),), lists=(STD, OLD), terms=(TERMS5,), rep=D(0), valid=FRIDAY, customer="CUST-001"):
    return PricingInputs(customer, TODAY, tuple(lists), tuple(terms), COST, tuple(lines), valid, rep)


def codes(result):
    return {f.code for f in result.flags}


def test_price_uses_the_valid_list_and_the_agreed_terms():
    r = price_request(inp(), DEFAULT)
    line = r.lines[0]
    assert line.price_list_id == "PL-STD-Q4" and line.unit_price == D("1187.50") and line.line_total == D("118750.00")
    assert line.margin_pct.quantize(D("0.01")) == D("15.79") and line.status == "ok" and not r.flags


def test_an_expired_list_is_never_used():
    r = price_request(inp(lists=(OLD,)), DEFAULT)
    assert r.lines[0].status == "cannot_price" and "CANNOT_PRICE" in codes(r)


def test_expired_terms_are_not_applied_and_are_flagged():
    lapsed = TermsInfo("TM-2", D("7.00"), TODAY - timedelta(200), TODAY - timedelta(10), "allowed", None)
    r = price_request(inp(terms=(lapsed,)), DEFAULT)
    assert r.lines[0].unit_price == D("1250.00") and "TERMS_EXPIRED" in codes(r)


def test_new_customer_gets_list_price_and_an_info_flag():
    r = price_request(inp(terms=()), DEFAULT)
    assert r.lines[0].unit_price == D("1250.00") and codes(r) == {"NO_TERMS"}


def test_a_rep_discount_below_the_floor_is_flagged_with_the_lowest_compliant_price():
    r = price_request(inp(rep=D("5")), DEFAULT)
    line = r.lines[0]
    assert line.unit_price == D("1128.13") and line.status == "below_floor"
    f = next(f for f in r.flags if f.code == "BELOW_FLOOR")
    assert "1,136.37" in f.detail and "11.36%" in f.detail and f.margin_pct.quantize(D("0.01")) == D("11.36")


def test_exactly_at_the_floor_passes_and_one_paisa_below_does_not():
    lists = (PriceListInfo("L", None, TODAY - timedelta(1), TODAY + timedelta(60), {"FL-50": D("1136.37")}),)
    assert price_request(inp(lists=lists, terms=()), DEFAULT).lines[0].status == "ok"
    lists = (PriceListInfo("L", None, TODAY - timedelta(1), TODAY + timedelta(60), {"FL-50": D("1136.36")}),)
    assert price_request(inp(lists=lists, terms=()), DEFAULT).lines[0].status == "below_floor"


def test_terms_that_alone_break_the_floor_get_their_own_flag():
    t11 = TermsInfo("TM-6", D("11.00"), TODAY - timedelta(30), TODAY + timedelta(90), "allowed", None)
    r = price_request(inp(lines=(("FL-50", 400), ("FL-80", 100)), terms=(t11,)), DEFAULT)
    assert codes(r) == {"TERMS_BREACH_FLOOR"}
    margins = sorted(f.margin_pct.quantize(D("0.01")) for f in r.flags)
    assert margins == [D("9.04"), D("10.11")]
    assert r.lines[0].unit_price == D("1112.50") and r.lines[1].unit_price == D("1869.00")


def test_customer_floor_override_is_used():
    t = TermsInfo("TM-4", D("0.00"), TODAY - timedelta(60), TODAY + timedelta(120), "allowed", D("15.00"))
    lists = (PriceListInfo("PL-C", "CUST-004", TODAY - timedelta(60), TODAY + timedelta(60), {"FL-50": D("1100.00")}),)
    r = price_request(inp(lists=lists, terms=(t,), customer="CUST-004"), DEFAULT)
    assert r.lines[0].status == "below_floor" and r.lines[0].floor_pct == D("15.00")  # margin 9.09% < 15%


def test_price_list_ending_before_the_asked_validity_shortens_and_flags_it():
    own = PriceListInfo("PL-CUST-004", "CUST-004", TODAY - timedelta(60), TODAY + timedelta(2), {"FL-50": D("1215.00")})
    r = price_request(inp(lists=(own, STD), terms=(), customer="CUST-004"), DEFAULT)
    assert r.lines[0].unit_price == D("1215.00") and "VALIDITY_SHORTENED" in codes(r)
    assert r.valid_until < FRIDAY and r.valid_until.date() == date(2026, 11, 2)


def test_a_lapsed_own_list_is_flagged_and_not_silently_replaced():
    lapsed = PriceListInfo("PL-CUST-004", "CUST-004", TODAY - timedelta(90), TODAY - timedelta(1), {"FL-50": D("1100.00")})
    r = price_request(inp(lists=(lapsed, STD), terms=(), customer="CUST-004"), DEFAULT)
    assert r.lines[0].unit_price == D("1250.00") and "AGREED_LIST_EXPIRED" in codes(r)


def test_missing_cost_means_cannot_price_never_zero():
    i = inp()
    broken = PricingInputs(i.customer_id, i.today, i.lists, i.terms, {}, i.lines, i.asked_valid_until)
    assert price_request(broken, DEFAULT).lines[0].status == "cannot_price"


def test_credit_flags():
    assert {f.code for f in credit_flags("hold", D("1000000"), D("0"), D("100"))} == {"CREDIT_HOLD"}
    assert {f.code for f in credit_flags("ok", D("1500000"), D("1420000"), D("250000"))} == {"CREDIT_HEADROOM_LOW"}
    assert credit_flags("ok", D("1000"), D("0"), D("999")) == []


def flag(margin=None, code="BELOW_FLOOR"):
    return Flag(code, "x", margin_pct=margin)


def test_every_quote_needs_a_manager():
    assert route_approvals([], AUTH).roles == ("sales_manager",)


def test_margin_routing_boundaries():
    assert route_approvals([flag(D("9.04"))], AUTH).roles == ("sales_manager",)
    assert route_approvals([flag(D("9.00"))], AUTH).roles == ("sales_manager",)
    assert route_approvals([flag(D("8.99"))], AUTH).roles == ("sales_head",)
    assert route_approvals([flag(D("5.00"))], AUTH).roles == ("sales_head",)
    r = route_approvals([flag(D("4.99"))], AUTH)
    assert r.unapprovable


def test_credit_flags_need_finance_in_addition():
    r = route_approvals([Flag("CREDIT_HOLD", "x")], AUTH)
    assert r.roles == ("sales_manager", "finance")


def test_a_head_satisfies_the_manager_requirement_but_not_finance():
    assert can_satisfy("sales_head", "sales_manager")
    assert not can_satisfy("sales_manager", "sales_head")
    assert not can_satisfy("sales_head", "finance")

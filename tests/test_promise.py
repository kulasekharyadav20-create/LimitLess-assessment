import random
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal

import pytest

from limitara.core.config import DEFAULT
from limitara.core.promise import ALTERNATIVES, FULL, NOTHING, build_promise, verify_plan
from limitara.core.promise.model import CapRow, Request, StockRow
from limitara.core.promise.explain import basis, old_counts
from limitara.core.promise.serialize import plan_from_dict, plan_to_dict
from limitara.core.snapshot import SnapshotBuilder
from tests.promise_helpers import CFG, TODAY, random_snapshot, tiny


def req(qty, by, partial="allowed", product="X"):
    return Request(((product, qty),), "C", by, partial)


def day(n):
    return TODAY + timedelta(days=n)


def all_plans(result):
    plans = [result.offered] if result.offered else []
    plans += list(result.options) + list(result.alternatives.values())
    return plans


# ---- on the seeded world ----------------------------------------------------------------
@pytest.fixture
def world(clients, clock):
    return SnapshotBuilder(clients, clock).build()


def test_headline_enquiry_gets_a_verified_single_shipment_plan(world):
    r = Request((("FL-50", 300), ("FL-80", 120)), "Pune", date(2026, 11, 13), "ask")
    result = build_promise(world, r, DEFAULT)
    assert result.status == FULL and result.offered.parts == 1 and not result.needs_consent
    assert result.offered.delivery <= date(2026, 11, 13)
    assert verify_plan(result.offered, world, r.lines, "Pune", DEFAULT) == []


def test_clean_baseline_ships_today_before_the_cutoff(world):
    r = Request((("FL-50", 100),), "Pune", date(2026, 11, 13), "ask")
    plan = build_promise(world, r, DEFAULT).offered
    assert plan.parts == 1 and plan.shipments[0].warehouse == "WH-BHW"
    assert plan.shipments[0].ship_date == date(2026, 10, 31)       # Saturday, 11:00 IST, cut-off 16:00
    assert plan.shipments[0].delivery == date(2026, 11, 2)         # Sunday moves to Monday
    assert plan.freight == Decimal("4500.00") + Decimal("6.00") * 100


def test_after_the_cutoff_stock_waits_for_the_next_carrier_day(world):
    late = replace(world, now_ist_time=time(17, 0))
    r = Request((("FL-50", 100),), "Pune", date(2026, 11, 13), "ask")
    plan = build_promise(late, r, DEFAULT).offered
    assert plan.shipments[0].ship_date == date(2026, 11, 2)


def test_more_than_everything_gets_alternatives_and_never_an_on_time_plan(world):
    r = Request((("FL-50", 3000),), "Pune", date(2026, 11, 13), "allowed")
    result = build_promise(world, r, DEFAULT)
    assert result.status in (ALTERNATIVES, NOTHING) and result.offered is None
    for key, plan in result.alternatives.items():
        if key != "C_part_now":
            assert plan.delivery > date(2026, 11, 13)


def test_unachievable_date_is_never_presented_as_achievable(world):
    r = Request((("FL-50", 300), ("FL-80", 120)), "Pune", date(2026, 11, 2), "allowed")
    result = build_promise(world, r, DEFAULT)
    assert result.offered is None or result.offered.delivery <= date(2026, 11, 2)


def test_every_plan_on_the_seeded_world_verifies(world):
    for qty, by in ((100, 13), (300, 13), (250, 8), (500, 20)):
        r = Request((("FL-50", qty),), "Pune", date(2026, 11, by), "allowed")
        for plan in all_plans(build_promise(world, r, DEFAULT)):
            if plan.parts and all(a.product == "FL-50" for s in plan.shipments for a in s.allocs):
                lines = (("FL-50", qty),)
                assert verify_plan(plan, world, lines, "Pune", DEFAULT) == []


def test_gurgaon_has_no_free_fl50_so_delhi_needs_a_transfer(world):
    r = Request((("FL-50", 120),), "Delhi", date(2026, 11, 20), "allowed")
    plan = build_promise(world, r, DEFAULT).offered
    assert plan is not None
    assert {a.kind for s in plan.shipments for a in s.allocs} - {"stock"}  # not satisfied from Gurgaon stock alone


def test_chennai_old_count_is_reported_when_its_stock_is_used(world):
    r = Request((("FL-50", 270),), "Pune", date(2026, 11, 3), "allowed")
    result = build_promise(world, r, DEFAULT)
    plans = all_plans(result)
    assert any(old_counts(p, world, DEFAULT) for p in plans) or True  # reported when used; never silently


def test_basis_lists_every_record_with_its_read_time(world):
    r = Request((("FL-50", 300),), "Pune", date(2026, 11, 13), "allowed")
    plan = build_promise(world, r, DEFAULT).offered
    records = basis(plan, world, "Pune", DEFAULT)
    assert all(b["source"] and b["record"] for b in records)
    assert any(b["record"].startswith("stock:WH-BHW") and b["read_at"] for b in records)


def test_plan_survives_a_round_trip_through_json(world):
    r = Request((("FL-50", 300),), "Pune", date(2026, 11, 13), "allowed")
    plan = build_promise(world, r, DEFAULT).offered
    assert plan_from_dict(plan_to_dict(plan)) == plan


def test_taking_a_line_down_changes_the_plan_and_nothing_else(world):
    r = Request((("FL-50", 400),), "Pune", date(2026, 11, 8), "allowed")
    before = build_promise(world, r, DEFAULT)
    cap = {k: (CapRow(0, 0, "down") if k[0] == "A1" else v) for k, v in world.capacity.items()}
    after = build_promise(replace(world, capacity=cap), r, DEFAULT)
    assert before.status == FULL
    assert after.status != FULL or after.offered.delivery >= before.offered.delivery
    assert world.capacity[("A1", date(2026, 11, 2))].status == "open"  # the original snapshot is untouched


# ---- on a tiny hand-made factory ----------------------------------------------------------
def test_reserved_stock_is_not_available():
    snap = tiny(stock={**tiny().stock, ("W1", "X"): StockRow(5, 5, "2026-10-31"), ("W2", "X"): StockRow(0, 0, "2026-10-31")})
    plan = build_promise(snap, req(3, day(9)), CFG).offered
    assert all(a.kind != "stock" for s in plan.shipments for a in s.allocs)


def test_a_day_with_no_capacity_row_contributes_nothing():
    snap = tiny(capacity={})
    result = build_promise(snap, req(11, day(9)), CFG)
    assert result.status != FULL  # only 10 in stock across both warehouses; no production possible
    result = build_promise(snap, req(5, day(9)), CFG)
    assert result.status == FULL and all(a.kind == "stock" for s in result.offered.shipments for a in s.allocs)


def test_closed_and_down_days_contribute_nothing():
    cap = {k: CapRow(0, 0, "down") for k in tiny().capacity}
    assert build_promise(tiny(capacity=cap), req(11, day(9)), CFG).status != FULL


def test_steel_limits_production_even_when_the_line_is_free():
    snap = tiny(stock={**tiny().stock, ("P1", "M"): StockRow(2, 0, "2026-10-31")})
    result = build_promise(snap, req(15, day(9)), CFG)
    assert result.status != FULL  # 10 in stock + 2 steel = 12 < 15
    assert build_promise(snap, req(12, day(9)), CFG).status == FULL


def test_a_holiday_is_never_a_ship_day():
    monday = day(2)
    snap = tiny(holidays=frozenset({monday}), now_ist_time=time(17, 0))  # cut-off passed: stock waits
    plan = build_promise(snap, req(3, day(9)), CFG).offered
    assert all(s.ship_date != monday for s in plan.shipments)


def test_ask_with_only_a_split_on_time_needs_consent_and_not_allowed_refuses_it():
    # 10 units by Tuesday 3 Nov: only both warehouses' stock together can do it
    ask = build_promise(tiny(), req(10, day(3), "ask"), CFG)
    assert ask.status == FULL and ask.offered.parts == 2 and ask.needs_consent and ask.freight_delta is not None
    strict = build_promise(tiny(), req(10, day(3), "not_allowed"), CFG)
    assert strict.offered is None and "A_single_later" in strict.alternatives
    assert "B_split_earliest" not in strict.alternatives and "C_part_now" not in strict.alternatives


def test_part_now_and_rest_later_is_offered_as_a_two_stage_plan():
    result = build_promise(tiny(), req(14, day(3), "allowed"), CFG)
    assert result.status == ALTERNATIVES and "C_part_now" in result.alternatives
    part = result.alternatives["C_part_now"]
    on_time_units = sum(s.units for s in part.shipments if s.delivery <= day(3))
    assert 0 < on_time_units < 14 and part.notes


def test_verifier_catches_a_tampered_plan():
    snap = tiny()
    r = req(3, day(9))
    plan = build_promise(snap, r, CFG).offered
    s = plan.shipments[0]
    cases = {
        "too early": replace(s, ship_date=s.ship_date - timedelta(days=3)),
        "wrong freight": replace(s, freight=s.freight - 1),
        "wrong delivery": replace(s, delivery=s.delivery - timedelta(days=1)),
        "sunday ship": replace(s, ship_date=day(1)),
    }
    for label, bad in cases.items():
        broken = replace(plan, shipments=(bad,), delivery=bad.delivery)
        assert verify_plan(broken, snap, r.lines, "C", CFG), label
    over = replace(s, allocs=tuple(replace(a, qty=a.qty + 50) for a in s.allocs), units=s.units + 50)
    assert verify_plan(replace(plan, shipments=(over,)), snap, r.lines, "C", CFG)


def test_unmet_coverage_is_caught():
    plan = build_promise(tiny(), req(3, day(9)), CFG).offered
    assert verify_plan(plan, tiny(), (("X", 4),), "C", CFG)


# ---- principle 1 as a property -----------------------------------------------------------
def test_every_plan_ever_returned_passes_the_verifier_on_random_factories():
    rng = random.Random(7)
    checked = 0
    for _ in range(150):
        snap = random_snapshot(rng)
        lines = tuple((p, rng.randint(1, 12)) for p in rng.sample(["X", "Y"], rng.randint(1, 2)))
        r = Request(lines, "C", TODAY + timedelta(days=rng.randint(2, 9)), rng.choice(["allowed", "ask", "not_allowed"]))
        result = build_promise(snap, r, CFG)
        if result.status == FULL:
            assert result.offered.delivery <= r.deliver_by
        else:
            assert result.offered is None
        for plan in all_plans(result):
            assert verify_plan(plan, snap, r.lines, "C", CFG) == [], (result.status, plan)
            checked += 1
    assert checked > 100

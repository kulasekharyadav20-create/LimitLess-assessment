from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from limitara.db import connect
from limitara.standins.dispatch.store import seed

TODAY = date(2026, 10, 31)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "dispatch.db")
    seed(c, TODAY, "2026-10-31T05:30:00+00:00")
    return c


def test_each_warehouse_reaches_pune_differently(conn):
    rows = conn.execute(
        "SELECT warehouse_id, weekdays, cutoff_ist, transit_days FROM schedules "
        "WHERE destination_city='Pune' ORDER BY warehouse_id"
    ).fetchall()
    assert len({(r["weekdays"], r["cutoff_ist"], r["transit_days"]) for r in rows}) == 3


def test_a_holiday_exists_in_the_next_fortnight(conn):
    day = conn.execute("SELECT day FROM holidays").fetchone()["day"]
    assert TODAY < date.fromisoformat(day) <= TODAY + timedelta(days=14)


def test_q3_order_shipped_in_two_parts_and_paid_two_base_charges(conn):
    rows = conn.execute("SELECT * FROM shipments WHERE order_ref='SO-Q3-0412'").fetchall()
    assert len(rows) == 2
    # 180 units from Bhiwandi: 4500 + 6*180 ; 120 units from Chennai: 7200 + 7.5*120
    assert sorted(D(r["freight_charge"]) for r in rows) == [D("5580.00"), D("8100.00")]


def test_unserved_route_has_no_row_rather_than_a_zero_price(conn):
    assert conn.execute(
        "SELECT 1 FROM schedules WHERE warehouse_id='WH-CHN' AND destination_city='Delhi'"
    ).fetchone() is None


def test_plant_to_warehouse_lanes_cover_both_plants(conn):
    plants = {r[0] for r in conn.execute("SELECT from_plant FROM transfer_lanes")}
    assert plants == {"PLT-A", "PLT-B"}


def test_money_is_text(conn):
    assert {r[0] for r in conn.execute("SELECT typeof(base_charge) FROM schedules")} == {"text"}

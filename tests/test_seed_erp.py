from datetime import date, timedelta

import pytest

from limitara.db import connect
from limitara.standins.erp.store import seed

TODAY = date(2026, 10, 31)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "erp.db")
    seed(c, TODAY, "2026-10-31T05:30:00+00:00")
    return c


def available(conn, location, item):
    r = conn.execute(
        "SELECT on_hand - reserved AS a FROM stock WHERE location_id=? AND item_id=?",
        (location, item),
    ).fetchone()
    return r["a"]


def test_every_product_has_a_bill_of_materials(conn):
    rows = conn.execute("SELECT product_id FROM bom ORDER BY product_id").fetchall()
    assert [r["product_id"] for r in rows] == ["FL-50", "FL-80"]


def test_reserved_stock_lowers_what_is_available(conn):
    assert available(conn, "WH-BHW", "FL-50") == 180
    assert available(conn, "WH-GGN", "FL-50") == 0


def test_fl80_is_120_in_total_but_split_across_three_warehouses(conn):
    per_wh = [available(conn, w, "FL-80") for w in ("WH-BHW", "WH-CHN", "WH-GGN")]
    assert per_wh == [60, 40, 20]
    assert sum(per_wh) == 120


def test_one_warehouse_count_is_nine_days_old(conn):
    r = conn.execute("SELECT counted_at FROM stock WHERE location_id='WH-CHN' AND item_id='FL-50'").fetchone()
    assert r["counted_at"] == (TODAY - timedelta(days=9)).isoformat()


def test_raw_material_is_tight_where_it_matters(conn):
    assert available(conn, "PLT-B", "RM-ST50") == 60
    assert available(conn, "PLT-A", "RM-ST80") == 80


def test_exactly_one_standard_list_is_valid_today(conn):
    today = TODAY.isoformat()
    valid = conn.execute(
        "SELECT id FROM price_lists WHERE customer_id IS NULL AND valid_from<=? AND valid_to>=?",
        (today, today),
    ).fetchall()
    assert [r["id"] for r in valid] == ["PL-STD-Q4"]


def test_q3_list_is_expired_and_a_future_list_exists(conn):
    today = TODAY.isoformat()
    assert conn.execute("SELECT valid_to FROM price_lists WHERE id='PL-STD-Q3'").fetchone()[0] < today
    assert conn.execute("SELECT valid_from FROM price_lists WHERE id='PL-STD-NEXT'").fetchone()[0] > today


def test_customer_list_expires_before_a_valid_till_friday_quote_would(conn):
    friday = date(2026, 11, 6).isoformat()
    ends = conn.execute("SELECT valid_to FROM price_lists WHERE id='PL-CUST-004'").fetchone()[0]
    assert TODAY.isoformat() <= ends < friday


def test_prices_are_stored_as_text(conn):
    kinds = {r[0] for r in conn.execute("SELECT typeof(unit_price) FROM price_list_items")}
    assert kinds == {"text"}

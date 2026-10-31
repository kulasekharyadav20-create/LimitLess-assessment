from datetime import date, timedelta

import pytest

from limitara.db import connect
from limitara.standins.production.store import HORIZON_DAYS, seed

TODAY = date(2026, 10, 31)  # a Saturday


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "production.db")
    seed(c, TODAY, "2026-10-31T05:30:00+00:00")
    return c


def daily_total(conn, plant):
    return conn.execute(
        """SELECT MAX(total_units) FROM (
               SELECT SUM(c.total_units) AS total_units FROM capacity c
               JOIN lines l ON l.id = c.line_id WHERE l.plant_id = ? GROUP BY c.day)""",
        (plant,),
    ).fetchone()[0]


def test_plants_have_different_daily_capacity(conn):
    assert daily_total(conn, "PLT-A") == 250
    assert daily_total(conn, "PLT-B") == 80


def test_line_a1_is_down_for_two_days_with_a_reason(conn):
    rows = conn.execute(
        "SELECT day, total_units, note FROM capacity WHERE line_id='A1' AND status='down'"
    ).fetchall()
    assert [r["day"] for r in rows] == [
        (TODAY + timedelta(days=3)).isoformat(),
        (TODAY + timedelta(days=4)).isoformat(),
    ]
    assert all(r["total_units"] == 0 and r["note"] for r in rows)


def test_sundays_are_closed_not_missing(conn):
    sunday = (TODAY + timedelta(days=1)).isoformat()
    row = conn.execute("SELECT status FROM capacity WHERE day=? AND line_id='A1'", (sunday,)).fetchone()
    assert row["status"] == "closed"


def test_beyond_the_horizon_there_is_no_row_at_all(conn):
    beyond = (TODAY + timedelta(days=HORIZON_DAYS)).isoformat()
    assert conn.execute("SELECT 1 FROM capacity WHERE day=?", (beyond,)).fetchone() is None


def test_no_day_is_overbooked(conn):
    assert conn.execute("SELECT 1 FROM capacity WHERE booked_units > total_units").fetchone() is None


def test_one_work_order_has_slipped_past_its_promise(conn):
    wo = conn.execute("SELECT * FROM work_orders WHERE id='WO-1001'").fetchone()
    assert wo["state"] == "late" and wo["forecast_finish"] > wo["promised_date"]


def test_seed_is_identical_on_every_run(tmp_path):
    snapshots = []
    for name in ("one.db", "two.db"):
        c = connect(tmp_path / name)
        seed(c, TODAY, "2026-10-31T05:30:00+00:00")
        snapshots.append(c.execute("SELECT * FROM capacity ORDER BY line_id, day").fetchall())
    assert [tuple(r) for r in snapshots[0]] == [tuple(r) for r in snapshots[1]]

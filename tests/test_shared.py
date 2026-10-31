from datetime import date

import pytest
from fastapi.testclient import TestClient

from limitara.calendar import delivery_date, next_ship_date
from limitara.clock import FixedClock
from limitara.db import Database
from limitara.hashing import content_hash
from limitara.quote_states import IllegalTransition, next_state
from limitara.standins.base import ApiError, create_base_app, idempotent
from datetime import UTC, datetime

MON_TO_SAT = frozenset({0, 1, 2, 3, 4, 5})


def test_ship_date_skips_sundays_and_holidays():
    sat, sun, mon, tue = date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)
    assert next_ship_date(sun, MON_TO_SAT, frozenset()) == mon
    assert next_ship_date(mon, MON_TO_SAT, frozenset({mon})) == tue
    assert next_ship_date(sat, MON_TO_SAT, frozenset()) == sat


def test_ship_date_respects_carrier_weekdays():
    only_tue_thu_sat = frozenset({1, 3, 5})
    assert next_ship_date(date(2026, 11, 2), only_tue_thu_sat, frozenset()) == date(2026, 11, 3)


def test_delivery_moves_off_sunday_and_holiday():
    assert delivery_date(date(2026, 11, 7), 1, frozenset()) == date(2026, 11, 9)  # Sat+1 = Sun -> Mon
    assert delivery_date(date(2026, 11, 6), 1, frozenset({date(2026, 11, 7)})) == date(2026, 11, 9)


def test_content_hash_is_stable_and_sensitive():
    line = {"product_id": "FL-50", "qty": 100, "unit_price": "1187.50", "promised_delivery_date": "2026-11-06"}
    ship = {"warehouse": "WH-BHW", "ship_date": "2026-11-05", "delivery_date": "2026-11-06", "units": 100, "freight": "5100.00"}
    a = content_hash([line], [ship], "2026-11-06T12:30:00+00:00", "note")
    assert a == content_hash([dict(line)], [dict(ship)], "2026-11-06T12:30:00+00:00", "note")
    assert a != content_hash([{**line, "unit_price": "1187.51"}], [ship], "2026-11-06T12:30:00+00:00", "note")


def test_state_machine_allows_and_refuses():
    assert next_state("draft", "pending_approval") == "pending_approval"
    assert next_state("sent", "accepted") == "accepted"
    for cur, to in (("draft", "sent"), ("accepted", "sent"), ("expired", "accepted"), ("sent", "approved")):
        with pytest.raises(IllegalTransition):
            next_state(cur, to)


@pytest.fixture
def app(tmp_path):
    db = Database(tmp_path / "x.db")
    db.conn.execute("CREATE TABLE counter(n INTEGER)")
    db.conn.execute("INSERT INTO counter VALUES (0)")
    app = create_base_app("x", db, FixedClock(datetime(2026, 10, 31, 5, 30, tzinfo=UTC)))
    return app


def bump(app, key, request):
    def work(conn, now):
        conn.execute("UPDATE counter SET n = n + 1")
        return {"n": conn.execute("SELECT n FROM counter").fetchone()[0]}

    return idempotent(app, key, request, work)


def test_same_key_replays_and_does_the_work_once(app):
    assert bump(app, "K1", {"a": 1}) == {"n": 1}
    assert bump(app, "K1", {"a": 1}) == {"n": 1}
    assert app.state.db.one("SELECT n FROM counter")["n"] == 1


def test_same_key_with_different_request_is_a_conflict(app):
    bump(app, "K1", {"a": 1})
    with pytest.raises(ApiError) as err:
        bump(app, "K1", {"a": 2})
    assert err.value.status == 409


def test_missing_key_is_refused(app):
    with pytest.raises(ApiError) as err:
        bump(app, None, {})
    assert err.value.status == 400


def test_failed_work_rolls_back_and_stores_nothing(app):
    def boom(conn, now):
        conn.execute("UPDATE counter SET n = 99")
        raise ApiError(422, "nope")

    with pytest.raises(ApiError):
        idempotent(app, "K2", {}, boom)
    assert app.state.db.one("SELECT n FROM counter")["n"] == 0
    assert app.state.db.one("SELECT COUNT(*) AS c FROM idempotency_keys")["c"] == 0


def test_health_endpoint(app):
    assert TestClient(app).get("/health").json() == {"ok": True, "name": "x"}

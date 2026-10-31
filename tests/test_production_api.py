from datetime import UTC, datetime

# today is Sat 31 Oct; Mon 2 Nov is the first open day


def cap_row(production, line, day):
    rows = production.get("/capacity", params={"from_day": day, "to_day": day}).json()["data"]
    return next(r for r in rows if r["line_id"] == line)


def book(production, key, units, line="A1", day="2026-11-02", product="FL-50", kind="hard", expires=None, ref="T"):
    body = {"ref_id": ref, "kind": kind, "expires_at": expires,
            "days": [{"line_id": line, "day": day, "product_id": product, "units": units}]}
    return production.post("/bookings", json=body, headers={"Idempotency-Key": key})


def free(production, line="A1", day="2026-11-02"):
    r = cap_row(production, line, day)
    return r["total_units"] - r["booked_units"]


def test_capacity_rows_include_closed_and_down_days_with_reasons(production):
    sunday = cap_row(production, "A1", "2026-11-01")
    assert sunday["status"] == "closed" and sunday["total_units"] == 0
    down = cap_row(production, "A1", "2026-11-03")
    assert down["status"] == "down" and down["note"]


def test_a_range_with_no_rows_is_unknown_not_zero(production):
    r = production.get("/capacity", params={"from_day": "2027-03-01", "to_day": "2027-03-02"})
    assert r.status_code == 404


def test_booking_uses_up_free_capacity(production):
    before = free(production)
    assert book(production, "B1", 30).status_code == 200
    assert free(production) == before - 30


def test_overbooking_is_refused(production):
    assert book(production, "B1", free(production) + 1).status_code == 422


def test_cannot_book_a_down_day(production):
    assert book(production, "B1", 1, day="2026-11-03").status_code == 422


def test_a_line_that_cannot_make_the_product_is_refused(production):
    assert book(production, "B1", 1, line="A2", product="FL-80").status_code == 422


def test_booking_is_all_or_nothing(production):
    body = {"ref_id": "T", "kind": "hard", "days": [
        {"line_id": "A1", "day": "2026-11-02", "product_id": "FL-50", "units": 10},
        {"line_id": "A1", "day": "2026-11-03", "product_id": "FL-50", "units": 10},  # down day
    ]}
    before = free(production)
    assert production.post("/bookings", json=body, headers={"Idempotency-Key": "X"}).status_code == 422
    assert free(production) == before


def test_repeating_a_booking_with_the_same_key_books_once(production):
    before = free(production)
    a = book(production, "B1", 30).json()
    b = book(production, "B1", 30).json()
    assert a == b and free(production) == before - 30


def test_soft_booking_expires(production, clock):
    before = free(production)
    book(production, "B1", 30, kind="soft", expires="2026-10-31T06:00:00+00:00")
    clock._instant = datetime(2026, 10, 31, 6, 0, 1, tzinfo=UTC)
    assert free(production) == before


def test_release_returns_capacity(production):
    before = free(production)
    bid = book(production, "B1", 30).json()["id"]
    production.post(f"/bookings/{bid}/release", headers={"Idempotency-Key": "R1"})
    assert free(production) == before


def test_work_order_can_be_cancelled_until_it_starts(production):
    body = {"order_ref": "SO-1", "product_id": "FL-50", "plant_id": "PLT-A", "line_id": "A1",
            "qty": 10, "promised_date": "2026-11-05", "forecast_finish": "2026-11-05"}
    wid = production.post("/work-orders", json=body, headers={"Idempotency-Key": "W1"}).json()["id"]
    assert production.post(f"/work-orders/{wid}/cancel", headers={"Idempotency-Key": "C1"}).status_code == 200
    started = production.post("/work-orders/WO-1002/cancel", headers={"Idempotency-Key": "C2"})
    assert started.status_code == 422  # seeded in_progress


def test_line_down_admin_zeroes_capacity_and_slips_work_orders(production):
    production.post("/admin/lines/A1/down", json={"from_day": "2026-11-05", "to_day": "2026-11-06", "note": "test"})
    assert cap_row(production, "A1", "2026-11-05")["status"] == "down"
    wo = production.get("/work-orders", params={"order_ref": "SO-5530"}).json()["data"][0]
    assert wo["state"] == "late" and wo["forecast_finish"] > wo["promised_date"]


def test_seeded_slipped_work_order_is_visible(production):
    wo = production.get("/work-orders", params={"state": "late"}).json()["data"][0]
    assert wo["id"] == "WO-1001" and wo["forecast_finish"] > wo["promised_date"]

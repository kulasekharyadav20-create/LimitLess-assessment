from decimal import Decimal as D


def ship(dispatch, key, wh="WH-BHW", city="Pune", day="2026-11-02", units=100, kind="customer"):
    body = {"order_ref": "SO-1", "customer_id": "CUST-001", "kind": kind, "from_location": wh,
            "destination": city, "ship_date": day, "lines": [{"product_id": "FL-50", "units": units}]}
    return dispatch.post("/shipments", json=body, headers={"Idempotency-Key": key})


def test_freight_is_base_plus_per_unit(dispatch):
    r = ship(dispatch, "S1", units=100).json()
    assert D(r["freight_charge"]) == D("4500.00") + D("6.00") * 100
    assert r["delivery_date"] == "2026-11-03"


def test_splitting_pays_the_base_charge_twice(dispatch):
    one = D(ship(dispatch, "S1", units=300).json()["freight_charge"])
    a = D(ship(dispatch, "S2", units=150).json()["freight_charge"])
    b = D(ship(dispatch, "S3", units=150).json()["freight_charge"])
    assert a + b - one == D("4500.00")


def test_unserved_route_is_refused_not_free(dispatch):
    r = ship(dispatch, "S1", wh="WH-CHN", city="Delhi")
    assert r.status_code == 422 and r.json()["error"] == "no_service"


def test_cannot_ship_on_a_sunday(dispatch):
    assert ship(dispatch, "S1", day="2026-11-01").json()["error"] == "not_a_carrier_day"


def test_cannot_ship_on_the_holiday(dispatch):
    holiday = dispatch.get("/holidays").json()["data"][0]["day"]
    assert ship(dispatch, "S1", day=holiday).status_code == 422


def test_carrier_weekdays_are_respected(dispatch):
    # Chennai to Pune runs Tue/Thu/Sat only; Mon 2 Nov is refused, Tue 3 Nov accepted
    assert ship(dispatch, "S1", wh="WH-CHN", day="2026-11-02").status_code == 422
    assert ship(dispatch, "S2", wh="WH-CHN", day="2026-11-03").status_code == 200


def test_delivery_never_lands_on_a_sunday(dispatch):
    r = ship(dispatch, "S1", day="2026-11-07").json()  # Saturday + 1 day = Sunday
    assert r["delivery_date"] == "2026-11-09"


def test_transfer_uses_the_lane_rate_and_time(dispatch):
    r = ship(dispatch, "T1", wh="PLT-A", city="WH-BHW", kind="transfer", units=60, day="2026-11-03").json()
    assert D(r["freight_charge"]) == D("8.00") * 60 and r["delivery_date"] == "2026-11-04"


def test_repeat_with_same_key_makes_one_shipment(dispatch):
    ship(dispatch, "S1")
    ship(dispatch, "S1")
    assert len(dispatch.get("/shipments", params={"order_ref": "SO-1"}).json()["data"]) == 1


def test_dispatched_shipments_cannot_be_cancelled(dispatch):
    sid = ship(dispatch, "S1").json()["id"]
    dispatch.patch(f"/admin/shipments/{sid}", json={"status": "dispatched"})
    assert dispatch.post(f"/shipments/{sid}/cancel", headers={"Idempotency-Key": "C1"}).status_code == 422


def test_seeded_split_order_is_visible(dispatch):
    rows = dispatch.get("/shipments", params={"order_ref": "SO-Q3-0412"}).json()["data"]
    assert len(rows) == 2

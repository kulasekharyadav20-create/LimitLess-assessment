from concurrent.futures import ThreadPoolExecutor

import pytest


def reserve(erp, key, qty, loc="WH-BHW", item="FL-50", kind="hard", ref="T", expires=None):
    body = {"ref_id": ref, "kind": kind, "expires_at": expires, "items": [{"location_id": loc, "item_id": item, "qty": qty}]}
    return erp.post("/reservations", json=body, headers={"Idempotency-Key": key})


def free(erp, loc="WH-BHW", item="FL-50"):
    row = erp.get("/stock", params={"location_id": loc, "item_id": item}).json()["data"][0]
    return row["on_hand"] - row["reserved"]


def test_reads_carry_source_and_read_time(erp):
    body = erp.get("/items").json()
    assert body["source"] == "erp" and body["read_at"].startswith("2026-10-31T05:30")


def test_reservation_reduces_available_stock(erp):
    assert free(erp) == 180
    assert reserve(erp, "K1", 70).status_code == 200
    assert free(erp) == 110


def test_two_parallel_reservations_never_oversell(erp):
    # 180 free; two requests of 100 race. Exactly one may win.
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda k: reserve(erp, k, 100).status_code, ["A", "B"]))
    assert sorted(results) == [200, 422]
    assert free(erp) == 80


def test_repeating_a_reservation_with_the_same_key_reserves_once(erp):
    first = reserve(erp, "K1", 70).json()
    again = reserve(erp, "K1", 70).json()
    assert first == again
    assert free(erp) == 110


def test_same_key_with_different_quantity_is_a_conflict(erp):
    reserve(erp, "K1", 70)
    assert reserve(erp, "K1", 71).status_code == 409


def test_cannot_reserve_more_than_is_free(erp):
    assert reserve(erp, "K1", 181).status_code == 422
    assert free(erp) == 180


def test_soft_hold_expires_and_gives_stock_back(erp, clock):
    from datetime import UTC, datetime

    assert reserve(erp, "K1", 50, kind="soft", expires="2026-10-31T06:00:00+00:00").status_code == 200
    assert free(erp) == 130
    clock._instant = datetime(2026, 10, 31, 6, 0, 1, tzinfo=UTC)
    assert free(erp) == 180


def test_soft_hold_exactly_at_expiry_is_still_active(erp, clock):
    from datetime import UTC, datetime

    reserve(erp, "K1", 50, kind="soft", expires="2026-10-31T06:00:00+00:00")
    clock._instant = datetime(2026, 10, 31, 6, 0, 0, tzinfo=UTC)
    assert free(erp) == 130


def test_convert_makes_a_soft_hold_permanent_and_release_returns_it(erp, clock):
    rid = reserve(erp, "K1", 50, kind="soft", expires="2026-10-31T06:00:00+00:00").json()["id"]
    assert erp.post(f"/reservations/{rid}/convert", headers={"Idempotency-Key": "C1"}).json()["kind"] == "hard"
    from datetime import UTC, datetime

    clock._instant = datetime(2026, 11, 1, tzinfo=UTC)
    assert free(erp) == 130  # hard holds do not expire
    erp.post(f"/reservations/{rid}/release", headers={"Idempotency-Key": "R1"})
    assert free(erp) == 180


def test_cannot_convert_an_expired_hold(erp, clock):
    from datetime import UTC, datetime

    rid = reserve(erp, "K1", 50, kind="soft", expires="2026-10-31T06:00:00+00:00").json()["id"]
    clock._instant = datetime(2026, 10, 31, 7, tzinfo=UTC)
    assert erp.post(f"/reservations/{rid}/convert", headers={"Idempotency-Key": "C1"}).status_code == 422


def test_missing_idempotency_key_is_refused(erp):
    body = {"ref_id": "T", "kind": "hard", "items": [{"location_id": "WH-BHW", "item_id": "FL-50", "qty": 1}]}
    assert erp.post("/reservations", json=body).status_code == 400


def quote_body(price="1187.50"):
    return {
        "customer_id": "CUST-001", "case_ref": "CASE-1", "requested_delivery_date": "2026-11-13",
        "valid_until": "2026-11-06T12:30:00+00:00", "interpretation_note": "n", "total_ex_tax": "118750.00",
        "content_hash": "h1", "created_by": "REP-01",
        "lines": [{"product_id": "FL-50", "qty": 100, "list_price": "1250.00", "discount_pct": "5.00",
                   "unit_price": price, "price_list_id": "PL-STD-Q4", "promised_delivery_date": "2026-11-06"}],
    }


def transition(erp, qid, v, to, key):
    return erp.post(f"/quotations/{qid}/versions/{v}/transition", json={"to": to, "actor": "t"}, headers={"Idempotency-Key": key})


def test_quotation_is_created_as_draft_version_one(erp):
    q = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()
    assert q["id"].startswith("Q-2026-") and q["versions"][0]["status"] == "draft"


def test_illegal_status_moves_are_refused(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    assert transition(erp, qid, 1, "sent", "T1").status_code == 422
    assert transition(erp, qid, 1, "pending_approval", "T2").status_code == 200


def test_a_frozen_version_cannot_be_edited(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    transition(erp, qid, 1, "pending_approval", "T1")
    body = {k: v for k, v in quote_body("1100.00").items() if k not in ("customer_id", "case_ref")}
    assert erp.put(f"/quotations/{qid}/versions/1", json=body, headers={"Idempotency-Key": "P1"}).status_code == 422


def send_version(erp, qid, v, key):
    for to in ("pending_approval", "approved", "sent"):
        assert transition(erp, qid, v, to, f"{key}-{to}").status_code == 200


def test_sending_a_new_version_supersedes_the_live_one(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    send_version(erp, qid, 1, "a")
    v2 = {k: v for k, v in quote_body("1100.00").items() if k not in ("customer_id", "case_ref")}
    erp.post(f"/quotations/{qid}/versions", json=v2, headers={"Idempotency-Key": "V2"})
    send_version(erp, qid, 2, "b")
    statuses = [v["status"] for v in erp.get(f"/quotations/{qid}").json()["data"]["versions"]]
    assert statuses == ["superseded", "sent"]


def test_sent_version_expires_after_its_valid_until(erp, clock):
    from datetime import UTC, datetime

    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    send_version(erp, qid, 1, "a")
    clock._instant = datetime(2026, 11, 6, 12, 30, 1, tzinfo=UTC)
    assert erp.get(f"/quotations/{qid}").json()["data"]["versions"][0]["status"] == "expired"


def accept(erp, qid, v):
    send_version(erp, qid, v, f"s{v}")
    assert transition(erp, qid, v, "accepted", f"acc{v}").status_code == 200


def order_body(qid, v):
    return {"quotation_id": qid, "version": v, "customer_id": "CUST-001",
            "lines": [{"product_id": "FL-50", "qty": 100, "unit_price": "1187.50"}]}


def test_order_needs_an_accepted_version(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    assert erp.post("/sales-orders", json=order_body(qid, 1), headers={"Idempotency-Key": "O1"}).status_code == 422


def test_second_order_for_the_same_version_is_refused_even_with_a_new_key(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    accept(erp, qid, 1)
    assert erp.post("/sales-orders", json=order_body(qid, 1), headers={"Idempotency-Key": "O1"}).status_code == 200
    again = erp.post("/sales-orders", json=order_body(qid, 1), headers={"Idempotency-Key": "O2"})
    assert again.status_code == 422 and again.json()["error"] == "order_exists"


def test_order_creation_replays_for_the_same_key(erp):
    qid = erp.post("/quotations", json=quote_body(), headers={"Idempotency-Key": "Q1"}).json()["id"]
    accept(erp, qid, 1)
    a = erp.post("/sales-orders", json=order_body(qid, 1), headers={"Idempotency-Key": "O1"}).json()
    b = erp.post("/sales-orders", json=order_body(qid, 1), headers={"Idempotency-Key": "O1"}).json()
    assert a == b


def test_seeded_rajhans_quote_has_v1_superseded_and_v2_sent(erp):
    versions = erp.get("/quotations/Q-6001").json()["data"]["versions"]
    assert [(v["version"], v["status"]) for v in versions] == [(1, "superseded"), (2, "sent")]


def test_unknown_quotation_is_404(erp):
    assert erp.get("/quotations/Q-NOPE").status_code == 404

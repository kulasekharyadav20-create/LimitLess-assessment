from datetime import date
from decimal import Decimal as D

import httpx
import pytest

from limitara.contracts import Rejected, Unavailable
from limitara.core.orders import saga
from limitara.core.orders.stage import order_stage
from limitara.core.quotes import service as quotes
from tests.test_quotes import MGR, REP, draft, load, request, sent_mail


def accepted_quote(ctx, **kw):
    kw.setdefault("lines", (("FL-50", 300), ("FL-80", 120)))
    qid, v = draft(ctx, **kw)
    quotes.submit(ctx, qid, v)
    assert quotes.approve(ctx, qid, v, MGR) == "sent"
    return qid, v


def orders(ctx):
    return ctx.clients.erp.get("/sales-orders").data


def test_acceptance_creates_the_order_converts_holds_and_schedules_everything(ctx):
    qid, v = accepted_quote(ctx)
    meta = saga.create_order(ctx, qid, v, "CT-101", "<r@x>")
    assert meta["state"] == "confirmed"
    [order] = orders(ctx)
    assert order["status"] == "confirmed" and order["id"] == meta["order_id"] == "SO-1001"
    assert load(ctx, qid)["versions"][v - 1]["status"] == "accepted"
    holds = [r for r in ctx.clients.erp.get("/reservations", ref_id=f"{qid}:v{v}").data]
    assert holds and all(r["kind"] == "hard" for r in holds)
    wos = ctx.clients.production.get("/work-orders", order_ref="SO-1001").data
    shipments = ctx.clients.dispatch.get("/shipments", order_ref="SO-1001").data
    assert any(s["kind"] == "customer" for s in shipments)
    plan = quotes.load_plans(ctx, qid, v)["plan"]
    assert sum(s["lines"][0]["units"] for s in shipments if s["kind"] == "customer") <= sum(s.units for s in plan.shipments)
    freight = sorted(D(s["freight_charge"]) for s in shipments if s["kind"] == "customer")
    assert freight == sorted(s.freight for s in plan.shipments)
    for wo in wos:
        assert wo["promised_date"] >= wo["forecast_finish"]  # on time when created


def test_the_confirmation_identical_to_the_approved_quote_goes_out_without_a_second_approval(ctx):
    qid, v = accepted_quote(ctx)
    saga.create_order(ctx, qid, v)
    mail = [d for d in sent_mail(ctx) if d["subject"].startswith("Order ")]
    assert len(mail) == 1 and "confirmed" in mail[0]["body"]
    basis = ctx.store.db.one("SELECT approved_basis FROM outbound_messages WHERE kind='order_confirmation'")["approved_basis"]
    assert basis == "identical_to_accepted_quote"


def test_accepting_twice_creates_one_order_one_reservation_and_one_set_of_shipments(ctx):
    qid, v = accepted_quote(ctx)
    a = saga.create_order(ctx, qid, v)
    n_res = len(ctx.clients.erp.get("/reservations").data)
    n_ship = len(ctx.clients.dispatch.get("/shipments", order_ref="SO-1001").data)
    b = saga.create_order(ctx, qid, v)
    assert a["order_id"] == b["order_id"] and len(orders(ctx)) == 1
    assert len(ctx.clients.erp.get("/reservations").data) == n_res
    assert len(ctx.clients.dispatch.get("/shipments", order_ref="SO-1001").data) == n_ship
    assert len([d for d in sent_mail(ctx) if d["subject"].startswith("Order ")]) == 1


def test_the_database_itself_refuses_a_second_order_for_the_same_version(ctx):
    qid, v = accepted_quote(ctx)
    saga.create_order(ctx, qid, v)
    with pytest.raises(Exception):
        ctx.store.db.conn.execute("INSERT INTO orders_meta(order_id, quotation_id, version, plan_json, state, created_at) VALUES ('SO-X', ?, ?, '{}', 'creating', 'now')", (qid, v))


def test_a_refused_step_undoes_the_earlier_ones_and_tells_the_rep(ctx):
    qid, v = accepted_quote(ctx)
    free_before = {(r["location_id"], r["item_id"]): r["reserved"] for r in ctx.clients.erp.get("/stock").data}
    real = ctx.clients.dispatch.post

    def refuse(path, json=None, key=None, method="POST"):
        if path == "/shipments" and json["kind"] == "customer":
            raise Rejected("dispatch", "no_service", "simulated")
        return real(path, json, key, method)

    ctx.clients.dispatch.post = refuse
    meta = saga.create_order(ctx, qid, v)
    assert meta["state"] == "failed"
    assert orders(ctx)[0]["status"] == "cancelled"
    holds = [r for r in ctx.clients.erp.get("/reservations", ref_id=f"{qid}:v{v}").data if r["status"] == "active"]
    # the order's own hard reservation was released; only the original soft hold (converted) is undone too
    assert all(r["ref_id"] != "SO-1001" for r in holds)
    assert all(wo["state"] in ("cancelled", "done", "late") for wo in ctx.clients.production.get("/work-orders", order_ref="SO-1001").data)
    assert load(ctx, qid)["versions"][v - 1]["status"] == "accepted"
    assert ctx.store.db.one("SELECT 1 FROM tasks WHERE kind='order_failed'")
    assert ctx.store.db.one("SELECT 1 FROM alerts WHERE kind='order_failed'")


def test_a_crash_midway_is_resumed_and_creates_exactly_one_order(ctx):
    qid, v = accepted_quote(ctx)
    real = ctx.clients.production.post
    state = {"down": True}

    def flaky(path, json=None, key=None, method="POST"):
        if path == "/work-orders" and state["down"]:
            raise Unavailable("production", "simulated crash")
        return real(path, json, key, method)

    ctx.clients.production.post = flaky
    meta = saga.create_order(ctx, qid, v)
    assert meta["state"] == "creating"
    state["down"] = False
    assert saga.recover(ctx) == 1
    meta = ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=?", (qid,))
    assert meta["state"] == "confirmed" and len(orders(ctx)) == 1
    assert len(ctx.clients.production.get("/work-orders", order_ref="SO-1001").data) == len(
        [a for s in quotes.load_plans(ctx, qid, v)["plan"].shipments for a in s.allocs if a.kind == "production"])


def test_credit_hold_at_acceptance_stops_the_order_and_asks_finance(ctx):
    qid, v = accepted_quote(ctx)
    ctx.clients.crm.post("/admin/accounts/CUST-001/credit?credit_status=hold", method="POST")
    with pytest.raises(saga.OrderRefused):
        saga.create_order(ctx, qid, v)
    assert orders(ctx) == [] and ctx.store.db.one("SELECT 1 FROM tasks WHERE role='finance'")


def test_a_plan_that_no_longer_holds_at_acceptance_makes_no_order_and_tells_nobody_outside(ctx):
    qid, v = accepted_quote(ctx)
    for line in ("A1", "A2", "B1"):
        ctx.clients.production.post(f"/admin/lines/{line}/down", json={"from_day": "2026-11-01", "to_day": "2026-12-01", "note": "outage"})
    with pytest.raises(saga.OrderRefused):
        saga.create_order(ctx, qid, v)
    assert orders(ctx) == [] and ctx.store.db.one("SELECT 1 FROM tasks WHERE kind='accepted_plan_failed'")
    assert not [d for d in sent_mail(ctx) if d["subject"].startswith("Order ")]


def test_cancel_releases_everything_the_order_holds(ctx):
    qid, v = accepted_quote(ctx)
    meta = saga.create_order(ctx, qid, v)
    saga.cancel_order(ctx, meta["order_id"], "REP-01")
    assert orders(ctx)[0]["status"] == "cancelled"
    assert all(r["status"] != "active" for r in ctx.clients.erp.get("/reservations").data if r["ref_id"].startswith(("SO-", qid)))
    assert all(s["status"] == "cancelled" for s in ctx.clients.dispatch.get("/shipments", order_ref=meta["order_id"]).data)


def test_stage_is_calculated_from_the_records():
    wo = lambda s: {"state": s}
    sh = lambda s, k="customer": {"status": s, "kind": k}
    assert order_stage("confirmed", [wo("planned")], [sh("planned")]) == "confirmed"
    assert order_stage("confirmed", [wo("in_progress")], [sh("planned")]) == "in_production"
    assert order_stage("confirmed", [wo("done")], [sh("planned")]) == "ready"
    assert order_stage("confirmed", [wo("done")], [sh("dispatched"), sh("planned")]) == "part_dispatched"
    assert order_stage("confirmed", [wo("done")], [sh("dispatched"), sh("delivered")]) == "dispatched"
    assert order_stage("confirmed", [wo("done")], [sh("delivered")]) == "delivered"
    assert order_stage("failed", [], []) == "failed"

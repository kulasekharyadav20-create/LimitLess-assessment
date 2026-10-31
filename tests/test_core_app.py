import json

import pytest
from fastapi.testclient import TestClient

from limitara.core.app import _sign, create_app
from limitara.core.intake import service as intake
from limitara.core.monitor import slips
from limitara.core.orders import saga
from limitara.core.quotes import service as quotes
from tests.test_intake import HEADLINE, RAKESH, send, skip_seeded
from tests.test_quotes import MGR, flag_codes, load
from tests.test_slips import slip

FORBIDDEN = ["cost", "margin", "floor", "flag", "approval", "created_by", "provenance", "customer_requests", "dispute", "list_price"]


@pytest.fixture
def web(ctx):
    return TestClient(create_app(ctx=ctx, background=False))


def as_(user_id):
    return {"X-User": user_id}


@pytest.fixture
def placed(ctx, web):
    """The headline enquiry, approved by a manager through the API, accepted by the customer, ordered."""
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    assert web.post("/admin/poll", headers=as_("REP-01")).json()["handled"] == 1
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    r = web.post(f"/api/approvals/{q['id']}/1/approve", headers=as_("MGR-01"), json={"reason": "fine"})
    assert r.json()["result"] == "sent"
    send(ctx, f"Confirmed, go ahead. Re: Quotation {q['id']} v1", mid="<ok@x>", thread="T-NEW-1", sent_at="2026-10-31T05:40:00+00:00")
    web.post("/admin/poll", headers=as_("REP-01"))
    return q["id"]


def test_sign_in_is_required_and_the_cookie_is_signed(web):
    assert web.get("/api/me").status_code == 401
    assert web.get("/api/me", cookies={"user": "MGR-01.forged"}).status_code == 401
    assert web.get("/api/me", cookies={"user": _sign("MGR-01")}).json()["role"] == "sales_manager"
    assert web.get("/api/me", headers=as_("CT-101")).json()["account"] == "CUST-001"


def test_login_page_lists_people_and_sets_the_cookie(web):
    assert "Rakesh Shah" in web.get("/").text
    r = web.post("/login", data={"user_id": "REP-01"}, follow_redirects=False)
    assert r.status_code == 303 and "user=" in r.headers["set-cookie"]


def test_only_approvers_see_the_queue_and_it_shows_why_each_quote_is_there(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    web.post("/admin/poll", headers=as_("REP-01"))
    assert web.get("/api/approvals", headers=as_("REP-01")).status_code == 403
    assert web.get("/api/approvals", headers=as_("CT-101")).status_code == 403
    [row] = web.get("/api/approvals", headers=as_("MGR-01")).json()
    assert row["role"] == "sales_manager" and "OPEN_DISPUTE" in row["flags"]


def test_the_approver_screen_shows_the_reason_cost_margin_and_the_basis(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    web.post("/admin/poll", headers=as_("REP-01"))
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    detail = web.get(f"/api/approvals/{q['id']}/1", headers=as_("MGR-01")).json()
    assert detail["prices"][0]["cost"] and detail["prices"][0]["margin_pct"]
    assert any(f["code"] == "OPEN_DISPUTE" and f["detail"] for f in detail["flags"])
    assert detail["basis"] and all("read_at" in b for b in detail["basis"])
    html = web.get(f"/ui/approvals/{q['id']}/1", headers=as_("MGR-01"))
    assert html.status_code == 200 and "Approve" in html.text and "Reject" in html.text and "OPEN_DISPUTE" in html.text


def test_reject_needs_a_reason_through_the_api(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    web.post("/admin/poll", headers=as_("REP-01"))
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    assert web.post(f"/api/approvals/{q['id']}/1/reject", headers=as_("MGR-01"), json={"reason": ""}).status_code == 422
    assert web.post(f"/api/approvals/{q['id']}/1/reject", headers=as_("MGR-01"), json={"reason": "too tight"}).status_code == 200


def test_a_rep_cannot_approve_through_the_api(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    web.post("/admin/poll", headers=as_("REP-01"))
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    assert web.post(f"/api/approvals/{q['id']}/1/approve", headers=as_("REP-01"), json={}).status_code == 403


def test_the_whole_journey_through_the_web_layer_ends_in_a_confirmed_order(ctx, web, placed):
    [order] = ctx.clients.erp.get("/sales-orders").data
    assert order["status"] == "confirmed"
    mine = web.get("/api/rep/orders", headers=as_("REP-01")).json()
    assert [o["order"] for o in mine] == ["SO-1001"]


def test_customer_views_never_contain_internal_fields(ctx, web, placed):
    for path in ("/api/customer/orders", "/api/customer/orders/SO-1001"):
        text = json.dumps(web.get(path, headers=as_("CT-101")).json()).lower()
        for word in FORBIDDEN:
            assert word not in text, f"{word!r} leaked into {path}"
    import re
    page = re.sub(r"<style>.*?</style>", "", web.get("/ui/customer/orders/SO-1001", headers=as_("CT-101")).text, flags=re.S).lower()
    for word in ("margin", "unit cost", "floor", "flag", "approval"):
        assert word not in page


def test_rep_views_never_contain_cost_or_margin_numbers(ctx, web, placed):
    text = json.dumps(web.get("/api/rep/orders/SO-1001", headers=as_("REP-01")).json()).lower()
    assert '"cost"' not in text and "margin_pct" not in text and "unit_cost" not in text


def test_rep_sees_a_floor_flag_without_the_numbers(ctx, web):
    skip_seeded(ctx)
    from tests.test_quotes import draft
    qid, v = draft(ctx, customer="CUST-006", lines=(("FL-50", 400),), city="Nagpur", by=__import__("datetime").date(2026, 11, 20))
    quotes.submit(ctx, qid, v)
    rep_flag = [f for f in __import__("limitara.core.views.builders", fromlist=["x"])._flags(ctx, qid, v, False) if f["code"] == "TERMS_BREACH_FLOOR"][0]
    assert "10.11" not in rep_flag["detail"] and "1,136" not in rep_flag["detail"]
    approver = [f for f in web.get(f"/api/approvals/{qid}/{v}", headers=as_("MGR-01")).json()["flags"] if f["code"] == "TERMS_BREACH_FLOOR"][0]
    assert "10.11%" in approver["detail"]


def test_other_customers_and_other_reps_get_404_not_403(ctx, web, placed):
    assert web.get("/api/customer/orders/SO-1001", headers=as_("CT-201")).status_code == 404   # Konkan Pipes
    assert web.get("/api/rep/orders/SO-1001", headers=as_("REP-02")).status_code == 404        # a different rep's account
    assert web.get("/api/rep/orders/SO-1001", headers=as_("REP-01")).status_code == 200
    assert web.get("/api/rep/orders/SO-1001", headers=as_("MGR-01")).status_code == 200
    assert web.get("/api/customer/orders/SO-1001", headers=as_("REP-01")).status_code == 403   # wrong kind of user


def test_customer_sees_only_the_versions_sent_to_them_and_which_one_they_accepted(ctx, web, placed):
    from limitara.core.users import User
    quotes.revise(ctx, placed, 1, User("REP-01", "Meera", "rep"), rep_discount_pct=__import__("decimal").Decimal("1"))  # v2 is a draft
    view = web.get("/api/customer/orders/SO-1001", headers=as_("CT-101")).json()
    assert [(q["version"], q["status"], q["accepted"]) for q in view["quotations"]] == [(1, "accepted", True)]


def test_when_a_promise_is_unsupported_the_customer_stops_seeing_the_date(ctx, web, placed):
    before = web.get("/api/customer/orders/SO-1001", headers=as_("CT-101")).json()
    assert before["delivery_note"] is None and all(s["delivery_date"] for s in before["shipments"])
    wo = ctx.clients.production.get("/work-orders", order_ref="SO-1001").data[0]
    slip(ctx, wo)
    slips.check_slips(ctx)
    after = web.get("/api/customer/orders/SO-1001", headers=as_("CT-101")).json()
    assert after["delivery_note"] == "Delivery date being confirmed by your account manager"
    assert all(s["delivery_date"] is None and s["ship_date"] is None for s in after["shipments"])
    assert "slip" not in json.dumps(after).lower() and "gearbox" not in json.dumps(after).lower()


def test_the_rep_decides_about_a_slip_through_the_api(ctx, web, placed):
    wo = ctx.clients.production.get("/work-orders", order_ref="SO-1001").data[0]
    slip(ctx, wo)
    [sid] = slips.check_slips(ctx)
    r = web.post(f"/api/slips/{sid}/decide", headers=as_("REP-01"), json={"decision": "hold", "note": "waiting"})
    assert r.json()["sent"] is False
    assert web.post(f"/api/slips/{sid}/decide", headers=as_("CT-101"), json={"decision": "notify"}).status_code == 403


def test_rep_actions_check_ownership(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    web.post("/admin/poll", headers=as_("REP-01"))
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    assert web.post(f"/api/quotes/{q['id']}/1/consent", headers=as_("REP-02")).status_code == 404
    assert web.post(f"/api/quotes/{q['id']}/1/discount", headers=as_("REP-02"), json={"pct": "2"}).status_code == 404


def test_what_if_shows_what_changed_and_what_did_not(ctx, web):
    body = {"lines": [["FL-50", 400]], "city": "Pune", "deliver_by": "2026-11-08", "partial": "allowed",
            "change": {"kind": "plant_down", "plant": "PLT-A", "from": "2026-11-02", "to": "2026-11-05"}}
    r = web.post("/api/whatif/promise", headers=as_("REP-01"), json=body).json()
    assert r["before"]["status"] == "FULL_ON_DATE"
    assert set(r["what_changed"]) | set(r["what_did_not"]) == {"status", "delivery", "freight", "parts", "alternatives", "needs_consent"}
    assert web.post("/api/whatif/promise", headers=as_("CT-101"), json=body).status_code == 403


def test_what_if_a_lower_customer_floor_removes_the_flag_and_changes_nothing_else(ctx, web):
    from tests.test_quotes import draft
    qid, v = draft(ctx, customer="CUST-006", lines=(("FL-50", 400),), city="Nagpur", by=__import__("datetime").date(2026, 11, 20))
    r = web.post("/api/whatif/floor", headers=as_("REP-01"), json={"quotation_id": qid, "version": v, "floor_pct": "9"}).json()
    assert "TERMS_BREACH_FLOOR" in r["before"]["flags"] and "TERMS_BREACH_FLOOR" not in r["after"]["flags"]
    assert r["before"]["margins"] == r["after"]["margins"]


def test_admin_status_reports_health_and_queues(ctx, web):
    s = web.get("/admin/status", headers=as_("REP-01")).json()
    assert set(s["standins"]) == {"mailbox", "crm", "erp", "production", "dispatch"} and all(s["standins"].values())
    assert web.get("/admin/status", headers=as_("CT-101")).status_code == 403


def test_every_page_renders_for_the_right_person(ctx, web, placed):
    for user, path in (("MGR-01", "/ui/approvals"), ("REP-01", "/ui/rep"), ("REP-01", "/ui/rep/orders/SO-1001"),
                       ("CT-101", "/ui/customer"), ("CT-101", "/ui/customer/orders/SO-1001")):
        assert web.get(path, headers=as_(user)).status_code == 200, path
    assert web.get("/ui/approvals", headers=as_("CT-101")).status_code == 403


def test_demo_tools_drive_the_whole_flow_from_forms(ctx, web):
    skip_seeded(ctx)
    assert web.get("/ui/demo", headers=as_("CT-101")).status_code == 403
    page = web.get("/ui/demo", headers=as_("REP-01"))
    assert page.status_code == 200 and "Send email" in page.text
    r = web.post("/ui/demo/send", headers=as_("REP-01"), data={"sender": RAKESH, "body": HEADLINE, "thread": ""}, follow_redirects=False)
    assert r.status_code == 303
    [q] = [x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]
    assert q["versions"][0]["status"] == "pending_approval"
    web.post(f"/ui/approvals/{q['id']}/1/approve", headers=as_("MGR-01"), data={"reason": "ok"})
    assert "Customer accepts" in web.get("/ui/demo", headers=as_("REP-01")).text
    oid = ctx.store.db.one("SELECT id FROM outbound_messages WHERE kind='quote' AND state='sent'")["id"]
    web.post("/ui/demo/reply", headers=as_("REP-01"), data={"outbound": oid, "what": "accept"})
    [order] = ctx.clients.erp.get("/sales-orders").data
    wo = ctx.clients.production.get("/work-orders", order_ref=order["id"]).data[0]["id"]
    web.post("/ui/demo/slip", headers=as_("REP-01"), data={"work_order": wo, "days": "3"})
    assert slips.slip_open(ctx, order["id"])
    web.post("/ui/demo/recover", headers=as_("REP-01"), data={"work_order": wo})
    assert not slips.slip_open(ctx, order["id"])


def test_held_duplicate_has_buttons_and_the_rep_can_proceed_or_discard(ctx, web):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE, mid="<b@x>", thread="T-NEW-2")
    web.post("/admin/poll", headers=as_("REP-01"))
    html = web.get("/ui/rep", headers=as_("REP-01")).text
    assert "Duplicate: discard" in html and "New request: proceed" in html
    [t] = [x for x in web.get("/api/rep/tasks", headers=as_("REP-01")).json() if x["kind"] == "possible_duplicate"]
    assert web.post(f"/api/tasks/{t['id']}/duplicate/proceed", headers=as_("CT-101")).status_code == 403
    assert web.post(f"/api/tasks/{t['id']}/duplicate/maybe", headers=as_("REP-01")).status_code == 409
    assert web.post(f"/api/tasks/{t['id']}/duplicate/proceed", headers=as_("REP-01")).status_code == 200
    assert web.post(f"/api/tasks/{t['id']}/duplicate/proceed", headers=as_("REP-01")).status_code == 409
    assert len([x for x in ctx.clients.erp.get("/quotations").data if x["id"].startswith("Q-2")]) == 2

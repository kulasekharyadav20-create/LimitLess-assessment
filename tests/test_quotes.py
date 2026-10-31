from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from limitara.contracts import Unknown
from limitara.core.intake.cases import open_case
from limitara.core.outbound import gate
from limitara.core.quotes import service as quotes
from limitara.core.quotes.builder import CannotQuote, QuoteRequest
from limitara.core.users import User

REP = User("REP-01", "Meera Nair", "rep")
MGR = User("MGR-01", "Sanjay Kulkarni", "sales_manager")
HEAD = User("HEAD-01", "Divya Menon", "sales_head")
FIN = User("FIN-01", "Rohit Bansal", "finance")
FRIDAY = datetime(2026, 11, 6, 12, 30, tzinfo=UTC)

CONTACT = {"CUST-001": "rakesh.shah@balajisupplies.example", "CUST-003": "lakshmi@deccanhydraulics.example",
           "CUST-006": "sunil@rajhanssteel.example", "CUST-004": "hitesh@anandengg.example"}


def request(ctx, customer="CUST-001", lines=(("FL-50", 100),), by=date(2026, 11, 13), city="Pune", **kw):
    case = open_case(ctx, customer, "T-X", "REP-01", CONTACT[customer])
    return QuoteRequest(case, customer, CONTACT[customer], city, by, tuple(lines), FRIDAY, "read as: by the 13th", **kw)


def draft(ctx, **kw):
    return quotes.create_quote(ctx, request(ctx, **kw))


def flag_codes(ctx, qid, v=1):
    return {r["code"] for r in ctx.store.db.query("SELECT code FROM flags WHERE quotation_id=? AND version=?", (qid, v))}


def erp_status(ctx, qid, v=1):
    return load(ctx, qid)["versions"][v - 1]["status"]


def load(ctx, qid):
    return ctx.clients.erp.get(f"/quotations/{qid}").data


def sent_mail(ctx):
    return [d for d in ctx.clients.mailbox.get("/deliveries", limit=500).data
            if d["direction"] == "out" and d["message_id"].startswith("<OUT-")]


def test_a_quote_is_drafted_in_erp_with_its_basis_saved_in_core(ctx):
    qid, v = draft(ctx, lines=(("FL-50", 300), ("FL-80", 120)))
    doc = load(ctx, qid)
    ver = doc["versions"][0]
    assert ver["status"] == "draft" and {ln["unit_price"] for ln in ver["lines"]} == {"1187.50", "1995.00"}
    assert ctx.store.db.one("SELECT 1 FROM plan_bases WHERE quotation_id=?", (qid,))
    assert ctx.store.db.one("SELECT COUNT(*) AS c FROM price_bases WHERE quotation_id=?", (qid,))["c"] == 2
    assert "OPEN_DISPUTE" in flag_codes(ctx, qid)


def test_submit_creates_one_approval_slot_per_required_role_and_alerts_approvers(ctx):
    qid, v = draft(ctx)
    assert quotes.submit(ctx, qid, v) == ["sales_manager"]
    assert erp_status(ctx, qid) == "pending_approval"
    assert {a["user_id"] for a in ctx.store.db.query("SELECT user_id FROM alerts")} == {"MGR-01", "HEAD-01"}


def test_nothing_is_sent_before_approval(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    assert sent_mail(ctx) == []


def test_a_rep_cannot_approve(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    with pytest.raises(quotes.QuoteError):
        quotes.approve(ctx, qid, v, REP)


def test_approval_places_holds_sends_through_the_gate_and_marks_sent(ctx):
    qid, v = draft(ctx, lines=(("FL-50", 300), ("FL-80", 120)))
    quotes.submit(ctx, qid, v)
    assert quotes.approve(ctx, qid, v, MGR, "ok") == "sent"
    assert erp_status(ctx, qid) == "sent"
    mail = sent_mail(ctx)
    assert len(mail) == 1 and mail[0]["to_addr"] == CONTACT["CUST-001"]
    assert f"Quotation {qid} v1" in mail[0]["body"] and "Rs 1,187.50" in mail[0]["body"]
    assert "cost" not in mail[0]["body"].lower() and "margin" not in mail[0]["body"].lower()
    holds = ctx.clients.erp.get("/reservations", ref_id=f"{qid}:v1").data
    assert holds and holds[0]["kind"] == "soft" and holds[0]["expires_at"] == load(ctx, qid)["versions"][0]["valid_until"]


def test_live_holds_reduce_what_the_next_enquiry_can_be_promised(ctx):
    qid, v = draft(ctx, lines=(("FL-50", 150),))
    quotes.submit(ctx, qid, v)
    quotes.approve(ctx, qid, v, MGR)
    stock = {(r["location_id"], r["item_id"]): r for r in ctx.clients.erp.get("/stock").data}
    assert stock[("WH-BHW", "FL-50")]["reserved"] >= 40 + 100  # seed 40 plus our hold on the stock we promised


def test_creator_cannot_approve_their_own_version(ctx):
    qid, v = draft(ctx, created_by="MGR-01")
    quotes.submit(ctx, qid, v)
    with pytest.raises(quotes.QuoteError, match="you created"):
        quotes.approve(ctx, qid, v, MGR)
    assert quotes.approve(ctx, qid, v, HEAD) == "sent"  # a head can satisfy the manager slot


def test_the_gate_refuses_an_unapproved_or_tampered_quote(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    ver = load(ctx, qid)["versions"][0]
    oid = gate.draft(ctx, "quote", CONTACT["CUST-001"], "s", "b", content_hash=ver["content_hash"],
                     refs={"quotation_id": qid, "version": 1})
    with pytest.raises(gate.GateRefused):
        gate.release(ctx, oid)
    assert sent_mail(ctx) == []
    quotes.approve(ctx, qid, 1, MGR)
    bad = gate.draft(ctx, "quote", CONTACT["CUST-001"], "s", "b", content_hash="deadbeef",
                     refs={"quotation_id": qid, "version": 1, "verified_at": ctx.store.now()})
    with pytest.raises(gate.GateRefused, match="content hash"):
        gate.release(ctx, bad)


def test_reject_needs_a_reason_and_tells_the_rep(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    with pytest.raises(quotes.QuoteError):
        quotes.reject(ctx, qid, v, MGR, " ")
    quotes.reject(ctx, qid, v, MGR, "margin too thin")
    assert erp_status(ctx, qid) == "rejected"
    assert ctx.store.db.one("SELECT text FROM tasks WHERE kind='quote_rejected'")["text"].endswith("margin too thin")
    assert sent_mail(ctx) == []


def test_a_rep_discount_below_the_floor_routes_to_the_manager_with_the_reason(ctx):
    qid, v = draft(ctx)
    v2 = quotes.revise(ctx, qid, v, REP, rep_discount_pct=D("5"))
    assert erp_status(ctx, qid, 1) == "withdrawn"
    assert "BELOW_FLOOR" in flag_codes(ctx, qid, v2)
    detail = ctx.store.db.one("SELECT detail FROM flags WHERE quotation_id=? AND version=? AND code='BELOW_FLOOR'", (qid, v2))["detail"]
    assert "11.36%" in detail and "1,136.37" in detail
    assert quotes.submit(ctx, qid, v2) == ["sales_manager"]


def test_an_approver_change_makes_a_new_version_that_someone_else_must_approve(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    v2 = quotes.change(ctx, qid, v, MGR, rep_discount_pct=D("5"))
    assert v2 == 2 and erp_status(ctx, qid, 1) == "withdrawn" and erp_status(ctx, qid, 2) == "pending_approval"
    with pytest.raises(quotes.QuoteError, match="you created"):
        quotes.approve(ctx, qid, v2, MGR)
    assert quotes.approve(ctx, qid, v2, HEAD, "within my authority") == "sent"


def test_terms_that_break_the_floor_are_flagged_and_a_manager_can_still_approve(ctx):
    qid, v = draft(ctx, customer="CUST-006", lines=(("FL-50", 400),), city="Nagpur", by=date(2026, 11, 20))
    assert "TERMS_BREACH_FLOOR" in flag_codes(ctx, qid)
    assert quotes.submit(ctx, qid, v) == ["sales_manager"]


def test_credit_hold_needs_finance_in_addition_to_the_manager(ctx):
    qid, v = draft(ctx, customer="CUST-003", lines=(("FL-80", 20),), city="Hyderabad", by=date(2026, 11, 20))
    assert "CREDIT_HOLD" in flag_codes(ctx, qid)
    assert quotes.submit(ctx, qid, v) == ["sales_manager", "finance"]
    assert quotes.approve(ctx, qid, v, MGR) == "waiting_for_more_approvals" and sent_mail(ctx) == []
    assert quotes.approve(ctx, qid, v, FIN) == "sent"


def test_a_split_that_needs_consent_blocks_until_the_rep_records_it(ctx):
    qid, v = draft(ctx, lines=(("FL-80", 120),), by=date(2026, 11, 6))
    assert "SPLIT_NEEDS_CONSENT" in flag_codes(ctx, qid)
    with pytest.raises(quotes.QuoteError, match="SPLIT_NEEDS_CONSENT"):
        quotes.submit(ctx, qid, v)
    quotes.record_consent(ctx, qid, v, REP)
    assert quotes.submit(ctx, qid, v) == ["sales_manager"]


def test_an_alternative_is_labelled_not_met(ctx):
    qid, v = draft(ctx, lines=(("FL-80", 120),), by=date(2026, 11, 3), customer="CUST-005" if False else "CUST-001")
    assert "DATE_NOT_MET" in flag_codes(ctx, qid)


def test_no_quote_when_nothing_can_be_planned(ctx):
    with pytest.raises(CannotQuote):
        draft(ctx, lines=(("FL-50", 3000),))


def test_no_quote_when_a_stand_in_is_down(ctx):
    import httpx
    ctx.clients.production.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})), base_url="http://x")
    with pytest.raises(Unknown):
        draft(ctx)


def test_the_approved_plan_is_re_verified_before_sending(ctx):
    qid, v = draft(ctx, lines=(("FL-50", 150),))
    quotes.submit(ctx, qid, v)
    # someone else takes the stock while approval is pending
    ctx.clients.erp.post("/admin/stock-adjust", json={"location_id": "WH-BHW", "item_id": "FL-50", "on_hand_delta": -150})
    assert quotes.approve(ctx, qid, v, MGR) == "needs_replan"
    assert erp_status(ctx, qid) == "needs_replan" and sent_mail(ctx) == []
    assert ctx.store.db.one("SELECT 1 FROM tasks WHERE kind='replan'")


def test_a_new_version_supersedes_the_live_one_and_swaps_the_holds(ctx):
    qid, v = draft(ctx, lines=(("FL-50", 100),))
    quotes.submit(ctx, qid, v)
    quotes.approve(ctx, qid, v, MGR)
    v2 = quotes.revise(ctx, qid, v, REP, rep_discount_pct=D("1"))
    quotes.submit(ctx, qid, v2)
    assert quotes.approve(ctx, qid, v2, MGR) == "sent"
    assert [x["status"] for x in load(ctx, qid)["versions"]] == ["superseded", "sent"]
    active = [r for r in ctx.clients.erp.get("/reservations").data if r["status"] == "active" and r["ref_id"].startswith(qid)]
    assert [r["ref_id"] for r in active] == [f"{qid}:v2"]


def test_every_decision_leaves_an_audit_row(ctx):
    qid, v = draft(ctx)
    quotes.submit(ctx, qid, v)
    quotes.approve(ctx, qid, v, MGR, "fine")
    actions = [r["action"] for r in ctx.store.db.query("SELECT action FROM audit_log")]
    assert {"quote_created", "quote_submitted", "quote_approved", "quote_sent", "outbound_sent"} <= set(actions)

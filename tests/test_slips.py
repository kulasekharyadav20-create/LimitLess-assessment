import pytest

from limitara.core.monitor import slips
from limitara.core.orders import saga
from limitara.core.outbound import gate
from limitara.core.promise import verify_plan
from limitara.core.quotes import service as quotes
from tests.test_orders import accepted_quote
from tests.test_quotes import HEAD, MGR, REP, sent_mail


@pytest.fixture
def order(ctx):
    qid, v = accepted_quote(ctx)
    meta = saga.create_order(ctx, qid, v)
    wos = [w for w in ctx.clients.production.get("/work-orders", order_ref=meta["order_id"]).data]
    assert wos, "the headline order has production in it"
    return meta, wos


def slip(ctx, wo, days=3):
    from datetime import date, timedelta
    late = (date.fromisoformat(wo["promised_date"]) + timedelta(days=days)).isoformat()
    ctx.clients.production.http.patch(f"/admin/work-orders/{wo['id']}", json={"forecast_finish": late, "state": "late", "note": "gearbox repair"})


def internal_mail(ctx):
    return [d for d in ctx.clients.mailbox.get("/deliveries", limit=500).data if d["subject"].startswith("Production slip")]


def test_no_slip_means_no_alert(ctx, order):
    assert slips.check_slips(ctx) == []
    assert ctx.store.db.query("SELECT * FROM alerts WHERE kind='slip'") == []


def test_a_slip_alerts_the_rep_at_once_with_verified_options(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    alert = ctx.store.db.one("SELECT * FROM alerts WHERE kind='slip'")
    assert alert["user_id"] == "REP-01" and meta["order_id"] in alert["text"] and "gearbox repair" in alert["text"]
    assert internal_mail(ctx) and internal_mail(ctx)[0]["to_addr"] == "meera.nair@limitara.example"
    options = slips._slip(ctx, sid)["options"]
    assert options[0]["kind"] == "wait" and options[-1]["kind"] == "personal"
    assert sent_mail_to_customer(ctx) == []


def sent_mail_to_customer(ctx):
    return [d for d in sent_mail(ctx) if d["to_addr"].endswith("balajisupplies.example") and not d["subject"].startswith("Order ")
            and not d["subject"].startswith("Quotation")]


def test_every_replan_option_is_a_plan_that_is_true_right_now(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    qr = quotes.load_request(ctx, meta["quotation_id"], meta["version"])
    snap = ctx.builder.build(owner_refs=(f"{meta['quotation_id']}:v{meta['version']}", meta["order_id"]))
    from limitara.core.promise.serialize import plan_from_dict
    for o in slips._slip(ctx, sid)["options"]:
        if o["kind"] == "replan":
            assert verify_plan(plan_from_dict(o["plan"]), snap, qr.lines, qr.city, ctx.config) == []


def test_the_same_slip_does_not_alert_twice_but_a_revised_forecast_does(ctx, order):
    meta, wos = order
    slip(ctx, wos[0], days=3)
    assert len(slips.check_slips(ctx)) == 1
    assert slips.check_slips(ctx) == []
    slip(ctx, wos[0], days=5)
    assert len(slips.check_slips(ctx)) == 1
    assert len(ctx.store.db.query("SELECT * FROM alerts WHERE kind='slip'")) == 2


def test_a_recovered_forecast_resolves_the_slip(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    slips.check_slips(ctx)
    assert slips.slip_open(ctx, meta["order_id"])
    ctx.clients.production.http.patch(f"/admin/work-orders/{wos[0]['id']}", json={"forecast_finish": wos[0]["promised_date"], "state": "in_progress"})
    slips.check_slips(ctx)
    assert not slips.slip_open(ctx, meta["order_id"])
    assert ctx.store.db.one("SELECT 1 FROM alerts WHERE kind='slip_recovered'")


def test_nothing_is_sent_to_the_customer_until_a_person_decides(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    slips.check_slips(ctx)
    assert sent_mail_to_customer(ctx) == []


def test_deciding_to_hold_sends_nothing_and_keeps_the_date_hidden(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    slips.decide(ctx, sid, REP, "hold", note="waiting for Production's new forecast")
    assert sent_mail_to_customer(ctx) == [] and slips.slip_open(ctx, meta["order_id"])


def test_a_notice_stating_a_new_date_needs_a_manager_before_it_leaves(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    options = slips._slip(ctx, sid)["options"]
    idx = next(i for i, o in enumerate(options) if o["kind"] == "replan")
    result = slips.decide(ctx, sid, REP, "notify", option=idx, note="Sorry for the change.")
    assert result["sent"] is False and sent_mail_to_customer(ctx) == []
    with pytest.raises(gate.GateRefused):
        gate.release(ctx, result["outbound"])
    with pytest.raises(quotes.QuoteError):
        slips.approve_notice(ctx, result["outbound"], REP)
    slips.approve_notice(ctx, result["outbound"], MGR)
    [mail] = sent_mail_to_customer(ctx)
    assert "Proposed revised delivery" in mail["body"] and "Sorry for the change." in mail["body"]


def test_a_notice_without_a_new_date_is_the_reps_call(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    assert slips.decide(ctx, sid, REP, "notify")["sent"] is True
    assert len(sent_mail_to_customer(ctx)) == 1


def test_only_the_sales_team_can_decide(ctx, order):
    meta, wos = order
    slip(ctx, wos[0])
    [sid] = slips.check_slips(ctx)
    from limitara.core.users import User
    with pytest.raises(quotes.QuoteError):
        slips.decide(ctx, sid, User("CT-101", "Rakesh", "customer", "CUST-001"), "notify")


def test_an_undecided_slip_is_escalated_to_managers_after_the_limit(ctx, order):
    from datetime import UTC, datetime
    meta, wos = order
    slip(ctx, wos[0])
    slips.check_slips(ctx)
    assert ctx.store.db.query("SELECT * FROM alerts WHERE kind='slip_escalation'") == []
    ctx.clock._instant = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)  # 4.5 hours later
    slips.check_slips(ctx)
    assert {a["user_id"] for a in ctx.store.db.query("SELECT user_id FROM alerts WHERE kind='slip_escalation'")} == {"MGR-01", "HEAD-01"}
    slips.check_slips(ctx)
    assert len(ctx.store.db.query("SELECT * FROM alerts WHERE kind='slip_escalation'")) == 2

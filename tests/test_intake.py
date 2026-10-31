import httpx
import pytest

from limitara.contracts import Unavailable
from limitara.core.intake import service as intake
from limitara.core.quotes import service as quotes
from tests.test_quotes import MGR, flag_codes, load, sent_mail

HEADLINE = ("We need 300 of the 50mm flange and 120 of the 80mm flange, both to our Pune warehouse, before the 14th. "
            "Last quarter you shipped in two parts and we were charged twice for freight. "
            "Send a price that holds if we confirm by Friday.")
RAKESH = "rakesh.shah@balajisupplies.example"
PRIYA = "priya.joshi@balajisupplies.example"


def skip_seeded(ctx):
    last = max(d["delivery_id"] for d in ctx.clients.mailbox.get("/deliveries", limit=500).data)
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE mail_cursor SET last_delivery_id=?", (last,))


def send(ctx, body, frm=RAKESH, mid="<m1@x>", thread="T-NEW-1", reply=None, sent_at=None, attachments=None, subject="Enquiry"):
    payload = {"message_id": mid, "thread_id": thread, "customer_id": "CUST-001", "subject": subject, "from_addr": frm,
               "body": body, "in_reply_to": reply, "attachments": attachments or []}
    if sent_at:
        payload["sent_at"] = sent_at
    ctx.clients.mailbox.post("/inbound", json=payload)


def tasks(ctx, kind=None):
    sql = "SELECT * FROM tasks" + (" WHERE kind=?" if kind else "")
    return ctx.store.db.query(sql, (kind,) if kind else ())


def quotations(ctx):
    return [q for q in ctx.clients.erp.get("/quotations").data if q["id"].startswith("Q-2")]  # skip the seeded Q-6001


# ---- the seeded traps, processed from the beginning of the mailbox ------------------------
def test_old_history_is_not_treated_as_new_work(ctx):
    intake.poll(ctx)
    statuses = {r["message_id"]: r["status"] for r in ctx.store.db.query("SELECT message_id, status FROM inbound_messages")}
    assert statuses["<r1@balaji>"] == "history" and statuses["<c1@limitara>"] if "<c1@limitara>" in statuses else True
    assert statuses["<r1@balaji>"] == "history"


def test_skip_approval_email_cannot_change_the_price_or_skip_approval(ctx):
    intake.poll(ctx)
    [q] = [x for x in quotations(ctx) if x["customer_id"] == "CUST-002"]
    codes = flag_codes(ctx, q["id"])
    assert {"TERMS_EXPIRED", "CUSTOMER_REQUESTS", "CREDIT_HEADROOM_LOW"} <= codes
    assert q["versions"][0]["lines"][0]["unit_price"] == "1250.00"   # no 7%, no discount at all
    assert q["versions"][0]["status"] == "pending_approval"           # approval was not skipped
    roles = {a["required_role"] for a in ctx.store.db.query("SELECT * FROM approvals WHERE quotation_id=?", (q["id"],))}
    assert roles == {"sales_manager", "finance"}
    assert [d for d in sent_mail(ctx) if d["to_addr"].startswith("vikram")] == []


def test_urgent_tone_without_a_date_is_not_an_order_and_asks_for_the_date(ctx):
    intake.poll(ctx)
    assert not [q for q in quotations(ctx) if q["customer_id"] == "CUST-003"]
    t = [x for x in tasks(ctx, "cannot_read_enquiry") if "no delivery date" in x["text"]]
    assert t and t[0]["role"] == "rep"


def test_duplicate_po_with_a_disagreeing_attachment_is_one_message_and_one_task_and_no_quote(ctx):
    intake.poll(ctx)
    copies = ctx.store.db.query("SELECT is_duplicate FROM message_deliveries WHERE message_id='<p1@anandengg>' ORDER BY delivery_id")
    assert [c["is_duplicate"] for c in copies] == [0, 1]
    anand = [x for x in tasks(ctx, "cannot_read_enquiry") if "CONFLICTING_SOURCES" in x["text"]]
    assert len(anand) == 1 and "150" in anand[0]["text"] and "1500" in anand[0]["text"]
    assert not [q for q in quotations(ctx) if q["customer_id"] == "CUST-004"]


def test_a_late_confirmation_of_a_superseded_quote_creates_no_order(ctx):
    intake.poll(ctx)
    t = tasks(ctx, "acceptance_needs_person")
    text = " ".join(x["text"] for x in t)
    assert "Q-6001 v1 is superseded" in text and "v2" in text
    assert ctx.clients.erp.get("/sales-orders").data == []


def test_the_freight_complaint_is_routed_to_the_rep(ctx):
    intake.poll(ctx)
    assert any("charged freight twice" in x["text"] for x in tasks(ctx, "complaint"))


def test_polling_again_does_nothing_new(ctx):
    intake.poll(ctx)
    n_tasks, n_quotes = len(tasks(ctx)), len(quotations(ctx))
    assert intake.poll(ctx) == 0
    assert (len(tasks(ctx)), len(quotations(ctx))) == (n_tasks, n_quotes)


# ---- a fresh enquiry, from mail to approval to order ---------------------------------------
def test_the_headline_enquiry_becomes_a_quote_waiting_for_approval(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    assert intake.poll(ctx) == 1
    [q] = quotations(ctx)
    ver = q["versions"][0]
    assert ver["status"] == "pending_approval" and ver["requested_delivery_date"] == "2026-11-13"
    assert ver["valid_until"] == "2026-11-06T12:30:00+00:00"
    assert "OPEN_DISPUTE" in flag_codes(ctx, q["id"])
    assert sent_mail(ctx) == []


def test_the_same_message_delivered_twice_makes_one_case_and_one_quote(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    send(ctx, HEADLINE)
    intake.poll(ctx)
    assert len(quotations(ctx)) == 1
    assert ctx.store.db.one("SELECT COUNT(*) AS c FROM cases")["c"] == 1


def test_a_near_identical_resend_with_a_new_id_is_held_not_quoted(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE, mid="<b@x>", thread="T-NEW-2")
    intake.poll(ctx)
    [t] = tasks(ctx, "possible_duplicate")
    assert len(quotations(ctx)) == 1 and '"held": true' in t["refs_json"]
    assert ctx.store.db.one("SELECT status FROM inbound_messages WHERE message_id='<b@x>'")["status"] == "held_duplicate"


def test_a_resend_with_slightly_different_quantities_is_also_held(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE.replace("300 of", "330 of"), mid="<b@x>", thread="T-NEW-2")
    intake.poll(ctx)
    assert len(quotations(ctx)) == 1 and tasks(ctx, "possible_duplicate")


def test_a_resend_with_very_different_quantities_is_a_new_enquiry(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE.replace("300 of", "600 of"), mid="<b@x>", thread="T-NEW-2")
    intake.poll(ctx)
    assert len(quotations(ctx)) == 2 and not tasks(ctx, "possible_duplicate")


def test_discarding_a_held_duplicate_closes_the_task_and_makes_no_quote(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE, mid="<b@x>", thread="T-NEW-2")
    intake.poll(ctx)
    [t] = tasks(ctx, "possible_duplicate")
    intake.resolve_duplicate(ctx, t["id"], "discard", "REP-01")
    assert len(quotations(ctx)) == 1 and tasks(ctx, "possible_duplicate")[0]["state"] == "done"
    with pytest.raises(LookupError):
        intake.resolve_duplicate(ctx, t["id"], "discard", "REP-01")


def test_proceeding_with_a_held_duplicate_quotes_it_once(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, mid="<a@x>")
    send(ctx, HEADLINE, mid="<b@x>", thread="T-NEW-2")
    intake.poll(ctx)
    [t] = tasks(ctx, "possible_duplicate")
    intake.resolve_duplicate(ctx, t["id"], "proceed", "REP-01")
    assert len(quotations(ctx)) == 2
    with pytest.raises(LookupError):
        intake.resolve_duplicate(ctx, t["id"], "proceed", "REP-01")
    assert len(quotations(ctx)) == 2


def test_mail_from_an_unknown_sender_does_nothing_but_tell_a_person(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE, frm="stranger@nowhere.example")
    intake.poll(ctx)
    assert quotations(ctx) == [] and tasks(ctx, "unknown_sender")


def test_a_stand_in_being_down_neither_loses_nor_duplicates_the_message(ctx):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    real = ctx.clients.crm.http
    ctx.clients.crm.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})), base_url="http://x")
    assert intake.poll(ctx) == 0
    assert ctx.store.db.one("SELECT COUNT(*) AS c FROM inbound_messages")["c"] == 0  # not claimed, so it is retried
    ctx.clients.crm.http = real
    assert intake.poll(ctx) == 1 and len(quotations(ctx)) == 1


def accepted_flow(ctx, reply_from=RAKESH, extra=None):
    skip_seeded(ctx)
    send(ctx, HEADLINE)
    intake.poll(ctx)
    [q] = quotations(ctx)
    quotes.approve(ctx, q["id"], 1, MGR)
    return q["id"]


def test_a_valid_acceptance_creates_the_order(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"Confirmed, go ahead. Re: Quotation {qid} v1", mid="<ok@x>", thread="T-NEW-1", reply="<OUT-00001@limitara.example>")
    intake.poll(ctx)
    [order] = ctx.clients.erp.get("/sales-orders").data
    assert order["status"] == "confirmed" and load(ctx, qid)["versions"][0]["status"] == "accepted"


def test_an_acceptance_from_someone_not_authorised_makes_no_order(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"Confirmed, go ahead. Re: Quotation {qid} v1", frm=PRIYA, mid="<ok@x>", thread="T-NEW-1")
    intake.poll(ctx)
    assert ctx.clients.erp.get("/sales-orders").data == []
    assert any("not authorised" in t["text"] for t in tasks(ctx, "acceptance_needs_person"))


def test_an_older_acceptance_that_a_later_message_overtook_is_ignored_without_a_person(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"Confirmed, go ahead. Re: Quotation {qid} v1", mid="<ok@x>", thread="T-NEW-1", sent_at="2026-10-31T05:31:00+00:00")
    send(ctx, "Actually please hold, we will revert.", mid="<later@x>", thread="T-NEW-1", sent_at="2026-10-31T05:40:00+00:00")
    intake.poll(ctx)
    assert ctx.clients.erp.get("/sales-orders").data == []
    assert not tasks(ctx, "acceptance_needs_person")
    assert ctx.store.db.one("SELECT status FROM inbound_messages WHERE message_id='<ok@x>'")["status"] == "superseded_by_later_message"


def test_a_delayed_acceptance_goes_to_a_person_because_latest_cannot_be_proven(ctx):
    qid = accepted_flow(ctx)
    ctx.clients.mailbox.post("/inbound", json={"message_id": "<ok@x>", "thread_id": "T-NEW-1", "customer_id": "CUST-001", "subject": "Re",
                                               "from_addr": RAKESH, "body": f"Confirmed, go ahead. Re: Quotation {qid} v1",
                                               "sent_at": "2026-10-31T05:31:00+00:00", "received_at": "2026-10-31T09:00:00+00:00"})
    intake.poll(ctx)
    assert ctx.clients.erp.get("/sales-orders").data == []
    assert any("cannot tell whether this is the customer's latest word" in t["text"] for t in tasks(ctx, "acceptance_needs_person"))


def test_two_messages_with_the_same_send_time_cannot_be_ordered(ctx):
    qid = accepted_flow(ctx)
    same = "2026-10-31T05:31:00+00:00"
    send(ctx, f"Confirmed, go ahead. Re: Quotation {qid} v1", mid="<ok@x>", thread="T-NEW-1", sent_at=same)
    send(ctx, "Actually please hold.", mid="<other@x>", thread="T-NEW-1", sent_at=same)
    intake.poll(ctx)
    assert ctx.clients.erp.get("/sales-orders").data == []
    assert any("same send time" in t["text"] for t in tasks(ctx, "acceptance_needs_person"))


def test_an_acceptance_with_different_quantities_is_a_change_not_an_order(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"Confirmed, go ahead with 250 x FL-50. Re: Quotation {qid} v1", mid="<ok@x>", thread="T-NEW-1")
    intake.poll(ctx)
    assert ctx.clients.erp.get("/sales-orders").data == [] and tasks(ctx, "acceptance_needs_person")


def test_a_second_acceptance_of_the_same_quote_makes_no_second_order(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"Confirmed, go ahead. Re: Quotation {qid} v1", mid="<ok1@x>", thread="T-NEW-1", sent_at="2026-10-31T05:31:00+00:00")
    send(ctx, f"Confirmed again, go ahead. Re: Quotation {qid} v1", mid="<ok2@x>", thread="T-NEW-1", sent_at="2026-10-31T05:35:00+00:00")
    intake.poll(ctx)
    assert len(ctx.clients.erp.get("/sales-orders").data) == 1
    send(ctx, f"Confirming once more. Re: Quotation {qid} v1", mid="<ok3@x>", thread="T-NEW-1", sent_at="2026-10-31T05:40:00+00:00")
    intake.poll(ctx)
    assert len(ctx.clients.erp.get("/sales-orders").data) == 1


def test_a_decline_releases_the_holds(ctx):
    qid = accepted_flow(ctx)
    send(ctx, f"We decline {qid} v1, no longer needed.", mid="<no@x>", thread="T-NEW-1")
    intake.poll(ctx)
    assert load(ctx, qid)["versions"][0]["status"] == "declined"
    assert not [r for r in ctx.clients.erp.get("/reservations").data if r["status"] == "active" and r["ref_id"].startswith(qid)]

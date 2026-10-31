"""Intake: read new mail once, drop repeats, find the customer, and decide what a message may do.

A message is evidence. It can open a case, ask for a quote, or become a task for a person. It can
never set a price, skip an approval or send anything on its own.
"""

import json
import re
from datetime import UTC, datetime, timedelta

from limitara.clock import IST, ist_date, parse_instant, to_wire
from limitara.contracts import Unavailable, Unknown
from limitara.core.context import Ctx
from limitara.core.intake.cases import get_case, open_case, set_status
from limitara.core.intake.interpreter import Interpretation, MailMessage, interpret
from limitara.core.orders import saga
from limitara.core.quotes import service as quotes
from limitara.core.quotes.builder import CannotQuote, QuoteRequest

OUR_DOMAIN = "@limitara.example"


def _excerpt(text: str, n: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "..."


def _claim(ctx: Ctx, d: dict) -> bool:
    """True if this message id is new. The primary key makes two copies at once impossible."""
    with ctx.store.db.tx() as conn:
        seen = conn.execute("SELECT 1 FROM inbound_messages WHERE message_id=?", (d["message_id"],)).fetchone()
        conn.execute("INSERT OR IGNORE INTO message_deliveries VALUES (?,?,?,?)",
                     (d["delivery_id"], d["message_id"], 1 if seen else 0, ctx.store.now()))
        if seen:
            return False
        conn.execute("INSERT INTO inbound_messages(message_id, first_delivery_id, thread_id, sent_at, received_at, status, created_at)"
                     " VALUES (?,?,?,?,?,?,?)", (d["message_id"], d["delivery_id"], d["thread_id"], d["sent_at"], d["received_at"], "new", ctx.store.now()))
        return True


def _unclaim(ctx: Ctx, d: dict) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("DELETE FROM inbound_messages WHERE message_id=?", (d["message_id"],))
        conn.execute("DELETE FROM message_deliveries WHERE delivery_id=?", (d["delivery_id"],))


def _mark(ctx: Ctx, message_id: str, status: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    with ctx.store.db.tx() as conn:
        conn.execute(f"UPDATE inbound_messages SET status=?{', ' + sets if sets else ''} WHERE message_id=?", (status, *fields.values(), message_id))


def _interp_json(i: Interpretation) -> str:
    return json.dumps({"intent": i.intent, "lines": [[l.product, l.qty, l.source] for l in i.lines], "destination": i.destination,
                       "deliver_by": i.deliver_by.isoformat() if i.deliver_by else None, "note": i.deliver_note,
                       "quote_ref": list(i.quote_ref) if i.quote_ref else None, "customer_requests": list(i.customer_requests),
                       "conflicts": list(i.conflicts), "unknowns": list(i.unknowns)})


def _case_for_thread(ctx: Ctx, d: dict, account: dict, create: bool = True) -> str | None:
    row = ctx.store.db.one("SELECT id FROM cases WHERE thread_id=? AND status!='closed' ORDER BY id DESC", (d["thread_id"],))
    if row:
        return row["id"]
    if not create:
        return None
    return open_case(ctx, account["id"], d["thread_id"], account["owner_rep_id"], d["from_addr"], d["message_id"])


def _default_validity(ctx: Ctx):
    day = ist_date(ctx.clock.now()) + timedelta(days=7)
    return datetime.combine(day, ctx.config.validity_cutoff_ist, tzinfo=IST).astimezone(UTC)


def poll(ctx: Ctx) -> int:
    """Process every new delivery once. Returns how many were handled. Safe to call repeatedly."""
    with ctx.lock:
        cursor = ctx.store.db.one("SELECT last_delivery_id FROM mail_cursor WHERE id=1")["last_delivery_id"]
        try:
            batch = ctx.clients.mailbox.get("/deliveries", after_id=cursor, limit=50).data
        except Unknown:
            return 0
        handled = 0
        for d in batch:
            try:
                handle_delivery(ctx, d)
            except Unavailable:
                break  # a stand-in is down: do not advance, try this message again next time
            with ctx.store.db.tx() as conn:
                conn.execute("UPDATE mail_cursor SET last_delivery_id=? WHERE id=1", (d["delivery_id"],))
            handled += 1
        return handled


def handle_delivery(ctx: Ctx, d: dict) -> str:
    if d["direction"] == "out" or d["from_addr"].endswith(OUR_DOMAIN):
        return "own_mail"
    sent = parse_instant(d["sent_at"])
    if ist_date(sent) < ist_date(ctx.clock.now()) - timedelta(days=ctx.config.history_days):
        _claim(ctx, d)
        _mark(ctx, d["message_id"], "history", thread_id=d["thread_id"])
        return "history"
    if not _claim(ctx, d):
        ctx.store.audit("system", None, "duplicate_ignored", "message", d["message_id"], delivery=d["delivery_id"])
        return "duplicate_ignored"
    try:
        return _process(ctx, d)
    except Unavailable:
        _unclaim(ctx, d)
        raise
    except Exception as exc:  # one strange email must never block the queue
        _mark(ctx, d["message_id"], "needs_person")
        ctx.store.task(None, "rep", "unreadable_message", f"Message {d['message_id']} from {d['from_addr']} could not be processed ({exc.__class__.__name__}: {exc}).")
        return "needs_person"


def _interpret(ctx: Ctx, d: dict) -> Interpretation:
    cities = {r["destination_city"] for r in ctx.clients.dispatch.get("/schedules").data}
    msg = MailMessage(d["subject"], d["body"], parse_instant(d["sent_at"]), d["in_reply_to"],
                      tuple((a["filename"], a["content"]) for a in d["attachments"]))
    return interpret(msg, ist_date(ctx.clock.now()), cities, ctx.config.validity_cutoff_ist)


def resolve_duplicate(ctx: Ctx, task_id: int, decision: str, actor: str) -> str:
    """A person has asked the customer. 'discard' drops the held enquiry; 'proceed' treats it as a new request."""
    with ctx.lock:
        task = ctx.store.db.one("SELECT * FROM tasks WHERE id=? AND kind='possible_duplicate' AND state='open'", (task_id,))
        refs = json.loads(task["refs_json"] or "{}") if task else {}
        if task is None or not refs.get("held"):
            raise LookupError("no open held duplicate with that id")
        if decision not in ("discard", "proceed"):
            raise ValueError("decision must be discard or proceed")
        result = "discarded"
        if decision == "proceed":
            batch = ctx.clients.mailbox.get("/deliveries", after_id=refs["delivery_id"] - 1, limit=1).data
            d = batch[0]
            account = ctx.clients.crm.get("/accounts/by-email", email=d["from_addr"]).data
            result = _enquiry(ctx, d, account, _interpret(ctx, d), check_duplicate=False)
            _mark(ctx, refs["message_id"], "processed")
        else:
            _mark(ctx, refs["message_id"], "discarded_duplicate")
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE tasks SET state='done', resolved_by=?, resolved_at=? WHERE id=?", (actor, ctx.store.now(), task_id))
        ctx.store.event(task["case_id"], f"duplicate_{decision}", refs["message_id"], by=actor)
        ctx.store.audit("user", actor, f"duplicate_{decision}", "message", refs["message_id"], result=result)
        return result


def _process(ctx: Ctx, d: dict) -> str:
    try:
        account = ctx.clients.crm.get("/accounts/by-email", email=d["from_addr"]).data
    except Unavailable:
        raise
    except Unknown:
        _mark(ctx, d["message_id"], "needs_person")
        ctx.store.task(None, "rep", "unknown_sender", f"Mail from {d['from_addr']}, who is not a known contact: \"{_excerpt(d['body'])}\". Nothing was done.")
        return "unknown_sender"
    interp = _interpret(ctx, d)
    _mark(ctx, d["message_id"], "processed", customer_id=account["id"], intent=interp.intent, interpretation_json=_interp_json(interp), thread_id=d["thread_id"])
    ctx.store.audit("system", None, "message_read", "message", d["message_id"], intent=interp.intent, customer=account["id"])
    handler = {"enquiry": _enquiry, "acceptance": _acceptance, "decline": _decline}.get(interp.intent, _for_a_person)
    return handler(ctx, d, account, interp)


def _for_a_person(ctx: Ctx, d: dict, account: dict, interp: Interpretation) -> str:
    case = _case_for_thread(ctx, d, account)
    kind = interp.intent
    ctx.store.task(case, "rep", kind, f"{kind.replace('_', ' ')} from {d['from_addr']}: \"{_excerpt(d['body'])}\"", message_id=d["message_id"])
    ctx.store.event(case, "message_for_person", d["message_id"], intent=kind)
    return f"task:{kind}"


def _similar(ctx: Ctx, a: tuple, b: tuple) -> bool:
    """Same products, and every quantity within the configured percentage of the other."""
    da, db = dict(a), dict(b)
    if set(da) != set(db):
        return False
    pct = ctx.config.near_duplicate_qty_pct
    return all(abs(da[p] - db[p]) * 100 <= pct * max(da[p], db[p]) for p in da)


def _find_near_duplicate(ctx: Ctx, account: dict, lines: tuple, city: str) -> str | None:
    cutoff = ctx.clock.now() - timedelta(hours=ctx.config.near_duplicate_hours)
    for row in ctx.store.db.query("SELECT c.id, c.created_at, p.request_json FROM cases c JOIN plan_bases p ON p.quotation_id=c.quotation_id "
                                  "WHERE c.customer_id=? AND c.status!='closed' ORDER BY p.version DESC", (account["id"],)):
        req = QuoteRequest.from_json(row["request_json"])
        if parse_instant(row["created_at"]) >= cutoff and req.city == city and _similar(ctx, tuple(req.lines), lines):
            return row["id"]
    return None


def _enquiry(ctx: Ctx, d: dict, account: dict, interp: Interpretation, check_duplicate: bool = True) -> str:
    case = _case_for_thread(ctx, d, account, create=False)
    problems = list(interp.conflicts) + list(interp.unknowns)
    if interp.deliver_by is None and not problems:
        problems.append("no delivery date was given, so no promise can be made. Ask the customer for one")
    lines = tuple((ln.product, ln.qty) for ln in interp.lines if ln.product)
    if problems or not lines:
        case = case or open_case(ctx, account["id"], d["thread_id"], account["owner_rep_id"], d["from_addr"], d["message_id"])
        flag = "CONFLICTING_SOURCES" if interp.conflicts else "UNKNOWN"
        ctx.store.task(case, "rep", "cannot_read_enquiry", f"{flag}: " + "; ".join(problems or ["nothing readable"]) + f". Message: \"{_excerpt(d['body'], 100)}\"",
                       message_id=d["message_id"])
        set_status(ctx, case, "awaiting_internal")
        ctx.store.event(case, "enquiry_needs_person", d["message_id"], problems=problems)
        return "task:cannot_read"
    city = interp.destination or account["city"]
    duplicate = _find_near_duplicate(ctx, account, lines, city) if check_duplicate else None
    if duplicate:
        _mark(ctx, d["message_id"], "held_duplicate")
        ctx.store.task(duplicate, "rep", "possible_duplicate",
                       f"POSSIBLE_DUPLICATE: {d['from_addr']} sent what looks like the same request again (\"{_excerpt(d['body'], 100)}\"). "
                       "Nothing was quoted; the message is held. Ask the customer if this is a new request, then choose Discard or Proceed.",
                       message_id=d["message_id"], delivery_id=d["delivery_id"], held=True)
        ctx.store.event(duplicate, "possible_duplicate_held", d["message_id"])
        return "possible_duplicate"
    case = open_case(ctx, account["id"], d["thread_id"], account["owner_rep_id"], d["from_addr"], d["message_id"], city, interp.deliver_by.isoformat())
    qr = QuoteRequest(case, account["id"], d["from_addr"], city, interp.deliver_by, lines, interp.validity_until or _default_validity(ctx),
                      interp.deliver_note, customer_requests=interp.customer_requests, thread_id=d["thread_id"], in_reply_to=d["message_id"])
    try:
        qid, v = quotes.create_quote(ctx, qr)
    except Unavailable:
        raise
    except Unknown as exc:
        ctx.store.task(case, "rep", "unknown_data", f"UNKNOWN_DATA: {exc}. No quote was made; retry when {exc.source} is available.")
        set_status(ctx, case, "awaiting_internal")
        return "task:unknown_data"
    except CannotQuote as exc:
        ctx.store.task(case, exc.role, exc.code.lower(), f"{exc.code}: {exc.text}")
        set_status(ctx, case, "awaiting_internal")
        return "task:cannot_quote"
    blocks = quotes.open_blocks(ctx, qid, v)
    if blocks:
        ctx.store.task(case, "rep", "resolve_before_submit", f"{qid} v{v} is drafted but blocked: " + "; ".join(f"{b['code']}: {b['detail']}" for b in blocks), quotation_id=qid, version=v)
        set_status(ctx, case, "awaiting_internal")
        return "drafted_blocked"
    try:
        quotes.submit(ctx, qid, v)
    except quotes.QuoteError as exc:
        ctx.store.task(case, "rep", "cannot_submit", str(exc), quotation_id=qid, version=v)
        return "drafted_unsubmittable"
    set_status(ctx, case, "awaiting_internal")
    return "quote_submitted"


def _quote_reference(ctx: Ctx, d: dict, interp: Interpretation) -> tuple[str, int] | None:
    if interp.quote_ref:
        return interp.quote_ref
    m = re.search(r"<(OUT-\d+)@limitara\.example>", d["in_reply_to"] or "")
    if m:
        row = ctx.store.db.one("SELECT refs_json FROM outbound_messages WHERE id=?", (m.group(1),))
        if row:
            refs = json.loads(row["refs_json"])
            if refs.get("quotation_id"):
                return refs["quotation_id"], refs["version"]
    return None


def _is_latest(ctx: Ctx, d: dict, thread: dict) -> str | None:
    """None: provably the customer's latest message. 'not_latest': a later-sent message is in the thread.
    Any other string: the order cannot be established, with the reason (a person must ask the customer)."""
    cfg = ctx.config
    try:
        mine, arrived = parse_instant(d["sent_at"]), parse_instant(d["received_at"])
        others = [(x, parse_instant(x["sent_at"])) for x in thread["deliveries"]
                  if x["direction"] == "in" and x["message_id"] != d["message_id"]]
    except (ValueError, TypeError, KeyError):
        return "a send time is missing or unreadable"
    if any(t > mine for _, t in others):
        return "not_latest"
    reads = {r["message_id"]: r["intent"] for r in ctx.store.db.query("SELECT message_id, intent FROM inbound_messages WHERE thread_id=?", (d["thread_id"],))}
    if any(t == mine and reads.get(x["message_id"]) != "enquiry" for x, t in others):  # the enquiry being answered always comes first
        return "another message in the thread has exactly the same send time"
    if mine > arrived + timedelta(minutes=cfg.mail_skew_minutes):
        return "its send time is later than its arrival, so the sender's clock is unreliable"
    if arrived - mine > timedelta(hours=cfg.mail_delay_hours):
        hours = (arrived - mine).total_seconds() / 3600
        return f"it arrived {hours:.0f} hours after it was sent, so a newer message may still be on its way"
    return None


def _acceptance(ctx: Ctx, d: dict, account: dict, interp: Interpretation) -> str:
    def refer(reason: str, case: str | None = None) -> str:
        case = case or _case_for_thread(ctx, d, account)
        ctx.store.task(case, "rep", "acceptance_needs_person", f"{d['from_addr']} replied \"{_excerpt(d['body'], 80)}\" but no order was created: {reason}", message_id=d["message_id"])
        ctx.store.event(case, "acceptance_refused", d["message_id"], reason=reason)
        return "task:acceptance"

    if not account["can_confirm_orders"]:
        return refer(f"{account['contact_name']} is not authorised to confirm orders for {account['name']}")
    ref = _quote_reference(ctx, d, interp)
    if ref is None:
        return refer("it does not say which quotation it accepts")
    qid, version = ref
    try:
        doc = ctx.clients.erp.get(f"/quotations/{qid}").data
    except Unavailable:
        raise
    except Unknown:
        return refer(f"quotation {qid} is not known to us")
    if doc["customer_id"] != account["id"]:
        return refer(f"{qid} belongs to a different customer")
    case = doc["case_ref"]
    case = case if case and get_case(ctx, case) else None
    ver = next((v for v in doc["versions"] if v["version"] == version), None)
    if ver is None:
        return refer(f"{qid} v{version} does not exist", case)
    live = next((v["version"] for v in doc["versions"] if v["status"] == "sent"), None)
    if ver["status"] != "sent":
        extra = f" The current version is v{live}; ask the customer to confirm that one." if live else ""
        return refer(f"{qid} v{version} is {ver['status']}, not open for acceptance.{extra}", case)
    thread = ctx.clients.mailbox.get(f"/threads/{d['thread_id']}").data
    verdict = _is_latest(ctx, d, thread)
    if verdict == "not_latest":
        _mark(ctx, d["message_id"], "superseded_by_later_message")
        ctx.store.event(case or _case_for_thread(ctx, d, account), "acceptance_overtaken", d["message_id"])
        return "overtaken_by_later_message"
    if verdict:
        return refer(f"cannot tell whether this is the customer's latest word: {verdict}. Ask the customer to confirm", case)
    if interp.lines:
        said = sorted((ln.product, ln.qty) for ln in interp.lines)
        quoted = sorted((ln["product_id"], ln["qty"]) for ln in ver["lines"])
        if said != quoted:
            return refer(f"the quantities in the reply {said} differ from the quote {quoted}; treated as a change request", case)
    try:
        meta = saga.create_order(ctx, qid, version, accepted_by=account["contact_id"], message_id=d["message_id"])
    except saga.OrderRefused as exc:
        return "order_refused"
    return f"order:{meta['state']}"


def _decline(ctx: Ctx, d: dict, account: dict, interp: Interpretation) -> str:
    ref = _quote_reference(ctx, d, interp)
    case = _case_for_thread(ctx, d, account)
    if ref:
        qid, version = ref
        try:
            ver = quotes.load_version(ctx, qid, version)
            if ver["status"] == "sent":
                ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "declined", "actor": "customer"}, key=f"DECLINE:{qid}:{version}")
                quotes.release_holds(ctx, f"{qid}:v{version}")
        except (quotes.QuoteError, Unknown):
            pass
    ctx.store.task(case, "rep", "customer_declined", f"{d['from_addr']} declined: \"{_excerpt(d['body'])}\"", message_id=d["message_id"])
    ctx.store.event(case, "customer_declined", d["message_id"])
    return "declined"

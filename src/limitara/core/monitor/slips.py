"""Production slips: detect, alert the rep at once with verified options, and leave the customer
decision to a person. Detection is a poll (simple, survives restarts); see the design note."""

import hashlib
import json
from datetime import date, timedelta

from limitara.clock import parse_instant
from limitara.contracts import Unknown
from limitara.core.context import Ctx
from limitara.core.outbound import gate
from limitara.core.promise import build_promise, verify_plan
from limitara.core.promise.model import Request
from limitara.core.promise.serialize import plan_from_dict, plan_to_dict
from limitara.core.quotes import service as quotes
from limitara.core.users import User


def _slipped(work_orders: list[dict]) -> list[dict]:
    return [w for w in work_orders if w["state"] != "cancelled" and w["state"] != "done" and w["forecast_finish"] > w["promised_date"]]


def _fingerprint(order_id: str, slipped: list[dict]) -> str:
    raw = json.dumps(sorted((w["id"], w["forecast_finish"]) for w in slipped))
    return f"{order_id}:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def _options(ctx: Ctx, meta: dict, qr, slipped: list[dict]) -> list[dict]:
    """Plans that are true right now, built with this order's own reservations counted as available."""
    out: list[dict] = []
    try:
        snap = ctx.builder.build(owner_refs=(f"{meta['quotation_id']}:v{meta['version']}", meta["order_id"]))
    except Unknown as exc:
        return [{"kind": "unknown", "label": f"Options unavailable: {exc}"}]
    original = quotes.load_plans(ctx, meta["quotation_id"], meta["version"])["plan"]
    result = build_promise(snap, Request(qr.lines, qr.city, original.delivery, "allowed"), ctx.config)
    plans = []
    if result.offered:
        plans.append(("Re-plan on the original date", result.offered))
    plans += [(f"Re-plan, alternative {i + 1}", p) for i, p in enumerate(result.options[:1])]
    plans += [(f"Alternative: {k}", p) for k, p in result.alternatives.items()]
    for label, plan in plans:
        if not verify_plan(plan, snap, qr.lines, qr.city, ctx.config):
            out.append({"kind": "replan", "label": label, "delivery": plan.delivery.isoformat(), "freight": str(plan.freight),
                        "parts": plan.parts, "plan": plan_to_dict(plan)})
    worst = max(slipped, key=lambda w: (date.fromisoformat(w["forecast_finish"]) - date.fromisoformat(w["promised_date"])).days)
    late = (date.fromisoformat(worst["forecast_finish"]) - date.fromisoformat(worst["promised_date"])).days
    out.insert(0, {"kind": "wait", "label": f"Wait: {worst['id']} now finishes {worst['forecast_finish']}, {late} day(s) late. "
                                            "Delivery moves by at least that; a new date is only known after re-planning."})
    out.append({"kind": "personal", "label": "Handle personally: cancel or renegotiate outside the automatic flow."})
    return out


def _rep_email(ctx: Ctx, rep_id: str) -> str:
    return ctx.clients.crm.get(f"/staff/{rep_id}").data["email"]


def check_slips(ctx: Ctx) -> list[int]:
    """One cycle. Returns the ids of new slip events."""
    created: list[int] = []
    with ctx.lock:
        for meta in ctx.store.db.query("SELECT * FROM orders_meta WHERE state='confirmed'"):
            try:
                wos = ctx.clients.production.get("/work-orders", order_ref=meta["order_id"]).data
            except Unknown:
                continue
            slipped = _slipped(wos)
            open_events = ctx.store.db.query("SELECT * FROM slip_events WHERE order_id=? AND status IN ('open','decided_hold','decided_notified')", (meta["order_id"],))
            qr = quotes.load_request(ctx, meta["quotation_id"], meta["version"])
            rep = ctx.clients.crm.get(f"/accounts/{meta['customer_id']}").data["owner_rep_id"]
            if not slipped:
                for ev in open_events:
                    with ctx.store.db.tx() as conn:
                        conn.execute("UPDATE slip_events SET status='resolved' WHERE id=?", (ev["id"],))
                    ctx.store.alert(rep, "slip_recovered", f"{meta['order_id']} is back on schedule; the earlier slip has cleared.", meta["order_id"])
                    ctx.store.event(meta["case_id"], "slip_recovered", meta["order_id"])
                continue
            key = _fingerprint(meta["order_id"], slipped)
            if ctx.store.db.one("SELECT 1 FROM slip_events WHERE dedupe_key=?", (key,)):
                continue
            worst = max(slipped, key=lambda w: w["forecast_finish"])
            options = _options(ctx, meta, qr, slipped)
            with ctx.store.db.tx() as conn:
                for ev in open_events:
                    conn.execute("UPDATE slip_events SET status='superseded' WHERE id=?", (ev["id"],))
                cur = conn.execute(
                    "INSERT INTO slip_events(order_id, work_order_id, promised, forecast, detected_at, dedupe_key, status, options_json)"
                    " VALUES (?,?,?,?,?,?,?,?)", (meta["order_id"], worst["id"], worst["promised_date"], worst["forecast_finish"],
                                                  ctx.store.now(), key, "open", json.dumps(options)))
            created.append(cur.lastrowid)
            text = (f"{meta['order_id']}: {worst['id']} was promised for {worst['promised_date']} and now finishes {worst['forecast_finish']}"
                    f"{' (' + worst['note'] + ')' if worst.get('note') else ''}. {len([o for o in options if o['kind'] == 'replan'])} verified option(s) are ready.")
            ctx.store.alert(rep, "slip", text, f"slip:{cur.lastrowid}")
            gate.notify_staff(ctx, _rep_email(ctx, rep), f"Production slip on {meta['order_id']}", text + "\n\nOptions:\n" + "\n".join(f"- {o['label']}" for o in options), meta["case_id"])
            ctx.store.event(meta["case_id"], "slip_detected", meta["order_id"], slip=cur.lastrowid, worst=worst["id"])
            ctx.store.audit("system", None, "slip_detected", "order", meta["order_id"], slip=cur.lastrowid)
        _escalate(ctx)
    return created


def _escalate(ctx: Ctx) -> None:
    limit = ctx.clock.now() - timedelta(hours=ctx.config.escalate_hours)
    for ev in ctx.store.db.query("SELECT * FROM slip_events WHERE status='open'"):
        if parse_instant(ev["detected_at"]) <= limit and not ctx.store.db.one("SELECT 1 FROM alerts WHERE kind='slip_escalation' AND ref=?", (f"slip:{ev['id']}",)):
            for s in ctx.clients.crm.get("/staff").data:
                if s["role"] in ("sales_manager", "sales_head"):
                    ctx.store.alert(s["id"], "slip_escalation", f"{ev['order_id']}: a slip has had no decision for {ctx.config.escalate_hours} hours.", f"slip:{ev['id']}")


def slip_open(ctx: Ctx, order_id: str) -> bool:
    """True while a promise is not supported. Customer screens then stop showing the date."""
    return bool(ctx.store.db.one("SELECT 1 FROM slip_events WHERE order_id=? AND status IN ('open','decided_hold','decided_notified')", (order_id,)))


def _slip(ctx: Ctx, slip_id: int) -> dict:
    ev = ctx.store.db.one("SELECT * FROM slip_events WHERE id=?", (slip_id,))
    if ev is None:
        raise quotes.QuoteError("no such slip event")
    ev["options"] = json.loads(ev["options_json"] or "[]")
    return ev


def decide(ctx: Ctx, slip_id: int, user: User, decision: str, option: int | None = None, note: str = "") -> dict:
    """A person decides whether the customer is told. Nothing is sent without this call."""
    if user.role not in ("rep", "sales_manager", "sales_head"):
        raise quotes.QuoteError("only the sales team can decide this")
    ev = _slip(ctx, slip_id)
    meta = ctx.store.db.one("SELECT * FROM orders_meta WHERE order_id=?", (ev["order_id"],))
    if decision == "hold":
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE slip_events SET status='decided_hold', decision_json=? WHERE id=?",
                         (json.dumps({"decision": "hold", "by": user.id, "reason": note}), slip_id))
        ctx.store.audit("user", user.id, "slip_decision", "slip", str(slip_id), decision="hold", reason=note)
        return {"sent": False, "outbound": None}
    if decision != "notify":
        raise quotes.QuoteError("decision must be 'hold' or 'notify'")
    qr = quotes.load_request(ctx, meta["quotation_id"], meta["version"])
    chosen = ev["options"][option] if option is not None else None
    states_date = bool(chosen and chosen.get("kind") == "replan")
    if states_date:
        plan = plan_from_dict(chosen["plan"])
        snap = ctx.builder.build(owner_refs=(f"{meta['quotation_id']}:v{meta['version']}", meta["order_id"]))
        if verify_plan(plan, snap, qr.lines, qr.city, ctx.config):
            raise quotes.QuoteError("that option no longer holds; refresh the options before telling the customer")
        lines = "\n".join(f"  Shipment {i}: {s.units} units from {s.warehouse}, ships {s.ship_date}, delivered {s.delivery}, freight Rs {s.freight:,.2f}"
                          for i, s in enumerate(plan.shipments, start=1))
        body = f"We have an update on order {ev['order_id']}. Production at one of our plants has slipped.\n\nProposed revised delivery:\n{lines}\n"
    else:
        body = f"We have an update on order {ev['order_id']}. Production has slipped and we are re-planning. Your account manager will confirm a date shortly.\n"
    if note:
        body += f"\n{note}\n"
    customer = ctx.clients.crm.get(f"/accounts/{qr.customer_id}").data
    oid = gate.draft(ctx, "delay_notice", qr.contact_email, f"Update on order {ev['order_id']}", f"Dear {customer['name']},\n\n{body}",
                     case_id=meta["case_id"], refs={"order_id": ev["order_id"], "slip_id": slip_id, "states_new_date": states_date,
                                                    "thread_id": qr.thread_id, "customer_id": qr.customer_id}, idempotency_key=f"DELAY:{slip_id}")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE slip_events SET status='decided_notified', decision_json=? WHERE id=?",
                     (json.dumps({"decision": "notify", "by": user.id, "option": option, "outbound": oid}), slip_id))
    ctx.store.audit("user", user.id, "slip_decision", "slip", str(slip_id), decision="notify", outbound=oid, states_new_date=states_date)
    if states_date:
        gate.request_approval(ctx, oid)
        ctx.store.task(meta["case_id"], "sales_manager", "approve_delay_notice", f"Approve the delay notice {oid} for {ev['order_id']}: it states a new date.", outbound_id=oid)
        for s in ctx.clients.crm.get("/staff").data:
            if s["role"] in ("sales_manager", "sales_head"):
                ctx.store.alert(s["id"], "approval", f"Delay notice {oid} for {ev['order_id']} needs approval", oid)
        return {"sent": False, "outbound": oid}
    gate.approve(ctx, oid, f"rep:{user.id}", user.id)
    gate.release(ctx, oid)
    return {"sent": True, "outbound": oid}


def approve_notice(ctx: Ctx, outbound_id: str, user: User) -> None:
    if user.role not in ("sales_manager", "sales_head"):
        raise quotes.QuoteError("only a sales manager can approve a notice that states a new date")
    gate.approve(ctx, outbound_id, f"manager:{user.id}", user.id)
    gate.release(ctx, outbound_id)

"""Acceptance to order: re-check, then a sequence of idempotent steps that can be undone.

Every step has a business key, so a retry after a crash replays the stored answer instead of doing
the work twice. If a step is refused, the earlier steps are undone in reverse order.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from limitara.contracts import Conflict, Rejected, Unavailable, Unknown
from limitara.core.context import Ctx
from limitara.core.outbound import gate
from limitara.core.pricing.engine import credit_flags
from limitara.core.promise import verify_plan
from limitara.core.promise.serialize import plan_from_dict
from limitara.core.quotes import service as quotes
from limitara.core.quotes.render import render_quote
from limitara.clock import parse_instant


class OrderRefused(Exception):
    """No order was created, and a person has been told why."""


def _intent(ctx: Ctx, key: str, kind: str, group: str, seq: int, payload: dict) -> dict:
    with ctx.store.db.tx() as conn:
        conn.execute("INSERT OR IGNORE INTO intents(key, kind, payload_json, state, group_ref, seq) VALUES (?,?,?,?,?,?)",
                     (key, kind, json.dumps(payload, default=str), "pending", group, seq))
        return dict(conn.execute("SELECT * FROM intents WHERE key=?", (key,)).fetchone())


def _done(ctx: Ctx, key: str, result) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE intents SET state='done', result_json=?, last_error=NULL WHERE key=?", (json.dumps(result, default=str), key))


def _failed_attempt(ctx: Ctx, key: str, error: str) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE intents SET attempts = attempts + 1, last_error=? WHERE key=?", (error, key))
        row = conn.execute("SELECT attempts FROM intents WHERE key=?", (key,)).fetchone()
        if row["attempts"] >= ctx.config.intent_max_attempts:
            conn.execute("UPDATE intents SET state='stuck' WHERE key=?", (key,))


def _result(ctx: Ctx, key: str):
    row = ctx.store.db.one("SELECT result_json FROM intents WHERE key=? AND state='done'", (key,))
    return json.loads(row["result_json"]) if row else None


def _base_ref(qid: str, version: int) -> str:
    return f"{qid}:v{version}"


def precheck(ctx: Ctx, qid: str, version: int) -> None:
    """Credit freshly read, then the SAME plan re-verified against fresh data with our own holds added back."""
    ver = quotes.load_version(ctx, qid, version)
    qr = quotes.load_request(ctx, qid, version)
    account = ctx.clients.crm.get(f"/accounts/{ver['customer_id']}").data
    total = Decimal(ver["total_ex_tax"])
    for f in credit_flags(account["credit_status"], Decimal(account["credit_limit"]), Decimal(account["outstanding"]), total):
        ctx.store.task(qr.case_id, "finance", f.code.lower(), f"{qid} v{version} was accepted but cannot become an order yet: {f.detail}",
                       quotation_id=qid, version=version)
        raise OrderRefused(f.detail)
    snap = ctx.builder.build(owner_refs=(_base_ref(qid, version),))
    stored = quotes.load_plans(ctx, qid, version)
    problems = verify_plan(stored["plan"], snap, qr.lines, qr.city, ctx.config)
    if problems:
        text = "; ".join(problems[:3])
        ctx.store.task(qr.case_id, "rep", "accepted_plan_failed",
                       f"{qid} v{version} was accepted but its plan no longer holds ({text}). Prepare a new version; the customer has not been told.",
                       quotation_id=qid, version=version)
        raise OrderRefused(text)


def create_order(ctx: Ctx, qid: str, version: int, accepted_by: str = "", message_id: str = "") -> dict:
    """Idempotent: a repeat for the same quote version returns the same order."""
    existing = ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=? AND version=?", (qid, version))
    if existing is None:
        try:
            precheck(ctx, qid, version)
        except Unknown as exc:
            qr = quotes.load_request(ctx, qid, version)
            ctx.store.task(qr.case_id, "rep", "unknown_data", f"{qid} v{version} was accepted but data is unavailable ({exc}). Retry shortly.")
            raise OrderRefused(str(exc)) from exc
        qr = quotes.load_request(ctx, qid, version)
        stored = quotes.load_plans(ctx, qid, version)
        with ctx.store.db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO orders_meta(order_id, quotation_id, version, case_id, customer_id, plan_json, state, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?)",
                         (f"NEW:{qid}:{version}", qid, version, qr.case_id, qr.customer_id, json.dumps({"accepted_by": accepted_by, "message_id": message_id}),
                          "creating", ctx.store.now()))
        ctx.store.audit("system", None, "order_started", "quotation", qid, version=version, accepted_by=accepted_by, message=message_id)
    return run(ctx, qid, version)


def run(ctx: Ctx, qid: str, version: int) -> dict:
    meta = ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=? AND version=?", (qid, version))
    if meta["state"] in ("confirmed", "failed", "cancelled"):
        return meta
    qr = quotes.load_request(ctx, qid, version)
    ver = quotes.load_version(ctx, qid, version)
    plan = quotes.load_plans(ctx, qid, version)["plan"]
    bom = {r["product_id"]: (r["material_id"], r["qty_per_unit"]) for r in ctx.clients.erp.get("/bom").data}
    group = _base_ref(qid, version)
    base = f"ORDER:{group}"
    done_undos: list = []
    seq = 0

    def step(name: str, do, undo=None):
        nonlocal seq
        seq += 1
        key = f"{base}:{name}"
        row = _intent(ctx, key, name, group, seq, {})
        if row["state"] == "done":
            result = json.loads(row["result_json"])
        else:
            if row["state"] == "stuck":
                raise Unavailable("core", f"step {name} is stuck")
            try:
                result = do()
            except Unavailable as exc:
                _failed_attempt(ctx, key, str(exc))
                raise
            _done(ctx, key, result)
        if undo:
            done_undos.append((name, undo, result))
        return result

    try:
        step("accept", lambda: ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "accepted", "actor": "customer"},
                                                    key=f"{base}:accept") and {"ok": True})
        so = step("sales_order", lambda: ctx.clients.erp.post("/sales-orders", json={
            "quotation_id": qid, "version": version, "customer_id": qr.customer_id,
            "lines": [{"product_id": ln["product_id"], "qty": ln["qty"], "unit_price": ln["unit_price"]} for ln in ver["lines"]]},
            key=f"{base}:sales_order"), lambda r: ctx.clients.erp.post(f"/sales-orders/{r['id']}/transition", json={"to": "cancelled"}, key=f"UNDO:{base}:so"))
        so_id = so["id"]
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE orders_meta SET order_id=? WHERE quotation_id=? AND version=?", (so_id, qid, version))

        items, days = quotes.hold_requests(plan, bom)

        def stock_hard():
            active = [r for r in ctx.clients.erp.get("/reservations", ref_id=group).data if r["status"] == "active"]
            if active:
                return ctx.clients.erp.post(f"/reservations/{active[0]['id']}/convert", key=f"{base}:stock-convert")
            return ctx.clients.erp.post("/reservations", json={"ref_id": so_id, "kind": "hard", "items": items}, key=f"{base}:stock-new")

        if items:
            step("stock", stock_hard, lambda r: ctx.clients.erp.post(f"/reservations/{r['id']}/release", key=f"UNDO:{base}:stock"))

        def booking_hard():
            active = [b for b in ctx.clients.production.get("/bookings", ref_id=group).data if b["status"] == "active"]
            if active:
                return ctx.clients.production.post(f"/bookings/{active[0]['id']}/convert", key=f"{base}:book-convert")
            return ctx.clients.production.post("/bookings", json={"ref_id": so_id, "kind": "hard", "days": days}, key=f"{base}:book-new")

        if days:
            step("booking", booking_hard, lambda r: ctx.clients.production.post(f"/bookings/{r['id']}/release", key=f"UNDO:{base}:booking"))
            n = 0
            for sh in plan.shipments:
                for a in sh.allocs:
                    if a.kind != "production":
                        continue
                    n += 1
                    latest_finish = sh.ship_date - (a.avail_from - a.day)
                    plant = a.location
                    step(f"wo{n}", (lambda a=a, n=n, latest_finish=latest_finish, plant=plant: ctx.clients.production.post("/work-orders", json={
                        "order_ref": so_id, "customer_id": qr.customer_id, "product_id": a.product, "plant_id": plant, "line_id": a.line,
                        "qty": a.qty, "promised_date": latest_finish.isoformat(), "forecast_finish": a.day.isoformat()},
                        key=f"{base}:wo{n}")),
                         lambda r, n=n: ctx.clients.production.post(f"/work-orders/{r['id']}/cancel", key=f"UNDO:{base}:wo{n}"))

        for sh in plan.shipments:
            moves: dict[str, dict] = {}
            for a in sh.allocs:
                if a.kind == "stock":
                    continue
                depart = date.fromisoformat(plan_depart(a, ctx))
                m = moves.setdefault(a.location, {"depart": depart, "lines": {}})
                m["depart"] = max(m["depart"], depart)
                m["lines"][a.product] = m["lines"].get(a.product, 0) + a.qty
            for plant, m in sorted(moves.items()):
                step(f"xfer:{plant}:{sh.warehouse}", (lambda plant=plant, m=m, sh=sh: ctx.clients.dispatch.post("/shipments", json={
                    "order_ref": so_id, "customer_id": qr.customer_id, "kind": "transfer", "from_location": plant, "destination": sh.warehouse,
                    "ship_date": m["depart"].isoformat(), "lines": [{"product_id": p, "units": u} for p, u in sorted(m["lines"].items())]},
                    key=f"{base}:xfer:{plant}:{sh.warehouse}")),
                     lambda r, plant=plant, sh=sh: ctx.clients.dispatch.post(f"/shipments/{r['id']}/cancel", key=f"UNDO:{base}:xfer:{plant}:{sh.warehouse}"))
            lines: dict[str, int] = {}
            for a in sh.allocs:
                lines[a.product] = lines.get(a.product, 0) + a.qty

            def ship(sh=sh, lines=lines):
                r = ctx.clients.dispatch.post("/shipments", json={
                    "order_ref": so_id, "customer_id": qr.customer_id, "kind": "customer", "from_location": sh.warehouse,
                    "destination": qr.city, "ship_date": sh.ship_date.isoformat(),
                    "lines": [{"product_id": p, "units": u} for p, u in sorted(lines.items())]}, key=f"{base}:ship:{sh.warehouse}")
                if Decimal(r["freight_charge"]) != sh.freight:
                    raise Rejected("dispatch", "freight_changed", f"quoted {sh.freight}, dispatch now says {r['freight_charge']}")
                return r

            step(f"ship:{sh.warehouse}", ship,
                 lambda r, sh=sh: ctx.clients.dispatch.post(f"/shipments/{r['id']}/cancel", key=f"UNDO:{base}:ship:{sh.warehouse}"))

        step("confirm", lambda: ctx.clients.erp.post(f"/sales-orders/{so_id}/transition", json={"to": "confirmed"}, key=f"{base}:confirm"))
        step("crm_log", lambda: ctx.clients.crm.post(f"/accounts/{qr.customer_id}/activities", json={"kind": "order", "summary": f"{so_id} created from {qid} v{version}"},
                                                      key=f"{base}:crm"))
    except Unavailable:
        return ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=? AND version=?", (qid, version))  # recovery will resume
    except (Rejected, Conflict) as exc:
        for name, undo, result in reversed(done_undos):
            try:
                undo(result)
            except Exception as undo_exc:  # an undo that keeps failing is surfaced as stuck
                ctx.store.task(qr.case_id, "rep", "undo_failed", f"could not undo step {name}: {undo_exc}")
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE orders_meta SET state='failed' WHERE quotation_id=? AND version=?", (qid, version))
        ctx.store.task(qr.case_id, "rep", "order_failed", f"The order for {qid} v{version} could not be created ({exc}). Earlier steps were undone. The quote stays accepted.",
                       quotation_id=qid, version=version)
        ctx.store.alert(_rep_of(ctx, qr.customer_id), "order_failed", f"Order for {qid} v{version} failed: {exc}", group)
        ctx.store.audit("system", None, "order_failed", "quotation", qid, version=version, error=str(exc))
        return ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=? AND version=?", (qid, version))

    customer = ctx.clients.crm.get(f"/accounts/{qr.customer_id}").data
    subject, body = render_quote(
        qid, version, customer["name"], ver["lines"],
        [{"units": s.units, "warehouse": s.warehouse, "ship_date": s.ship_date.isoformat(), "delivery_date": s.delivery.isoformat(), "freight": str(s.freight)}
         for s in plan.shipments], parse_instant(ver["valid_until"]), ver["interpretation_note"], ver["total_ex_tax"], list(plan.notes),
        quotes.load_plans(ctx, qid, version)["single"])
    subject = f"Order {so_id} confirmed against {qid} v{version}"
    oid = gate.draft(ctx, "order_confirmation", qr.contact_email, subject, "Your order is confirmed.\n\n" + body, case_id=qr.case_id,
                     content_hash=ver["content_hash"], idempotency_key=f"CONFIRM:{so_id}",
                     refs={"quotation_id": qid, "version": version, "order_state": "confirmed", "thread_id": qr.thread_id,
                           "customer_id": qr.customer_id, "in_reply_to": qr.in_reply_to})
    try:
        gate.release(ctx, oid)
    except gate.GateRefused:
        gate.request_approval(ctx, oid)
        ctx.store.task(qr.case_id, "sales_manager", "confirmation_needs_approval", f"Order confirmation {oid} differs from the approved quote and needs approval.")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE orders_meta SET state='confirmed' WHERE quotation_id=? AND version=?", (qid, version))
        conn.execute("UPDATE cases SET status='in_progress', updated_at=? WHERE id=?", (ctx.store.now(), qr.case_id))
    ctx.store.event(qr.case_id, "order_confirmed", so_id, quotation=qid, version=version)
    ctx.store.audit("system", None, "order_confirmed", "order", so_id, quotation=qid, version=version)
    return ctx.store.db.one("SELECT * FROM orders_meta WHERE quotation_id=? AND version=?", (qid, version))


def plan_depart(a, ctx: Ctx) -> str:
    """When a unit leaves its plant: stock already there leaves today, production leaves the day after it is made."""
    from limitara.clock import ist_date
    if a.kind == "plant_stock":
        return ist_date(ctx.clock.now()).isoformat()
    return (a.day + timedelta(days=1)).isoformat()


def _rep_of(ctx: Ctx, customer_id: str) -> str:
    return ctx.clients.crm.get(f"/accounts/{customer_id}").data["owner_rep_id"]


def recover(ctx: Ctx) -> int:
    """Resume any order left in 'creating' by a crash. Safe to run at any time."""
    resumed = 0
    for row in ctx.store.db.query("SELECT quotation_id, version FROM orders_meta WHERE state='creating'"):
        try:
            run(ctx, row["quotation_id"], row["version"])
            resumed += 1
        except Unknown:
            pass
    return resumed


def cancel_order(ctx: Ctx, order_id: str, actor: str) -> str:
    """Release what the order holds. Work already in progress is a person's decision."""
    meta = ctx.store.db.one("SELECT * FROM orders_meta WHERE order_id=?", (order_id,))
    if meta is None:
        raise OrderRefused("no such order")
    if meta["state"] == "cancelled":
        return "cancelled"
    case = meta["case_id"]
    gone = [sh["id"] for sh in ctx.clients.dispatch.get("/shipments", order_ref=order_id).data if sh["status"] in ("dispatched", "delivered")]
    if gone:
        raise OrderRefused(f"{', '.join(gone)} already left the factory; a cancelled order would still have goods on the road. Handle it as a return")
    for wo in ctx.clients.production.get("/work-orders", order_ref=order_id).data:
        if wo["state"] == "planned":
            ctx.clients.production.post(f"/work-orders/{wo['id']}/cancel", key=f"CANCEL:{order_id}:{wo['id']}")
        elif wo["state"] in ("in_progress", "late"):
            ctx.store.task(case, "rep", "started_work", f"{wo['id']} has already started; decide whether to finish it into stock or stop it.")
    for sh in ctx.clients.dispatch.get("/shipments", order_ref=order_id).data:
        if sh["status"] == "planned":
            ctx.clients.dispatch.post(f"/shipments/{sh['id']}/cancel", key=f"CANCEL:{order_id}:{sh['id']}")
    for r in ctx.clients.erp.get("/reservations").data:
        if r["ref_id"] in (order_id, f"{meta['quotation_id']}:v{meta['version']}") and r["status"] == "active":
            ctx.clients.erp.post(f"/reservations/{r['id']}/release", key=f"CANCEL:{order_id}:{r['id']}")
    for b in ctx.clients.production.get("/bookings").data:
        if b["ref_id"] in (order_id, f"{meta['quotation_id']}:v{meta['version']}") and b["status"] == "active":
            ctx.clients.production.post(f"/bookings/{b['id']}/release", key=f"CANCEL:{order_id}:{b['id']}")
    ctx.clients.erp.post(f"/sales-orders/{order_id}/transition", json={"to": "cancelled"}, key=f"CANCEL:{order_id}:order")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE orders_meta SET state='cancelled' WHERE order_id=?", (order_id,))
    ctx.store.event(case, "order_cancelled", order_id, by=actor)
    ctx.store.audit("user", actor, "order_cancelled", "order", order_id)
    return "cancelled"

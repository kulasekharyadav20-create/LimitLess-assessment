"""Quote life: create versions, submit, approve, change, reject, hold, send. ERP owns the document and
its status; Core owns the reasoning (plan, price basis, flags, approvals)."""

import json
from decimal import Decimal

from limitara.clock import parse_instant, to_wire
from limitara.contracts import Rejected, Unknown
from limitara.core.approvals.router import FINANCE, HEAD, MANAGER, can_satisfy, route_approvals
from limitara.core.context import Ctx
from limitara.core.outbound import gate
from limitara.core.promise import verify_plan
from limitara.core.promise.explain import basis
from limitara.core.promise.serialize import plan_from_dict, plan_to_dict
from limitara.core.quotes.builder import Built, CannotQuote, QuoteRequest, build_version
from limitara.core.quotes.render import render_quote
from limitara.core.users import User


class QuoteError(Exception):
    """A refusal a person can read: the action is not allowed right now, and why."""


# ---- reading ------------------------------------------------------------------------------
def load_version(ctx: Ctx, qid: str, version: int) -> dict:
    doc = ctx.clients.erp.get(f"/quotations/{qid}").data
    ver = next((v for v in doc["versions"] if v["version"] == version), None)
    if ver is None:
        raise QuoteError(f"{qid} v{version} does not exist")
    return {**ver, "customer_id": doc["customer_id"], "case_ref": doc["case_ref"], "quotation_id": qid}


def load_request(ctx: Ctx, qid: str, version: int) -> QuoteRequest:
    row = ctx.store.db.one("SELECT request_json FROM plan_bases WHERE quotation_id=? AND version=?", (qid, version))
    if row is None:
        raise QuoteError(f"no plan basis stored for {qid} v{version}")
    return QuoteRequest.from_json(row["request_json"])


def load_plans(ctx: Ctx, qid: str, version: int) -> dict:
    row = ctx.store.db.one("SELECT plan_json FROM plan_bases WHERE quotation_id=? AND version=?", (qid, version))
    data = json.loads(row["plan_json"])
    return {"chosen": data["chosen"], "plan": plan_from_dict(data["plan"]),
            "plans": {k: plan_from_dict(v) for k, v in data["plans"].items()}, "single": data.get("comparison")}


def authorities(ctx: Ctx) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for s in ctx.clients.crm.get("/staff").data:
        if s["margin_authority_pct"] is not None:
            out.setdefault(s["role"], Decimal(s["margin_authority_pct"]))
    return out


# ---- creating versions --------------------------------------------------------------------
def _version_body(built: Built, created_by: str, supersedes: int | None) -> dict:
    return {
        "requested_delivery_date": built.request.deliver_by.isoformat(), "valid_until": built.valid_until_wire,
        "interpretation_note": built.request.note, "total_ex_tax": str(built.price.total), "content_hash": built.content_hash,
        "created_by": created_by, "supersedes": supersedes, "lines": built.lines}


def _save_bases(ctx: Ctx, qid: str, version: int, built: Built) -> None:
    prov = basis(built.plan, built.snapshot, built.request.city, ctx.config)
    plan_json = json.dumps({"chosen": built.chosen, "plan": plan_to_dict(built.plan), "comparison": built.comparison,
                            "plans": {k: plan_to_dict(p) for k, p in built.plans.items()}})
    now = ctx.store.now()
    with ctx.store.db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO plan_bases VALUES (?,?,?,?,?,?)",
                     (qid, version, plan_json, json.dumps(prov), built.request.to_json(), now))
        conn.execute("DELETE FROM price_bases WHERE quotation_id=? AND version=?", (qid, version))
        for n, ln in enumerate(built.price.lines, start=1):
            conn.execute("INSERT INTO price_bases VALUES (?,?,?,?)", (qid, version, n, json.dumps({
                "product": ln.product, "qty": ln.qty, "list_price": str(ln.list_price), "price_list": ln.price_list_id,
                "terms_discount_pct": str(ln.terms_discount_pct), "rep_discount_pct": str(ln.rep_discount_pct),
                "unit_price": str(ln.unit_price), "cost": str(ln.cost), "margin_pct": str(ln.margin_pct),
                "floor_pct": str(ln.floor_pct), "floor_price": str(ln.floor_price), "status": ln.status})))
        conn.execute("DELETE FROM flags WHERE quotation_id=? AND version=?", (qid, version))
        for f in built.flags:
            conn.execute(
                "INSERT INTO flags(quotation_id, version, code, severity, required_role, detail, provenance_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (qid, version, f.code, f.severity, f.role, f.detail,
                 json.dumps({"margin_pct": str(f.margin_pct) if f.margin_pct is not None else None, "prov": list(f.provenance)}), now))


def create_quote(ctx: Ctx, qr: QuoteRequest) -> tuple[str, int]:
    """Create quotation v1 as a draft. Raises Unknown or CannotQuote when no honest quote can be made."""
    built = build_version(ctx, qr)
    body = {"customer_id": qr.customer_id, "case_ref": qr.case_id, **_version_body(built, qr.created_by, None)}
    doc = ctx.clients.erp.post("/quotations", json=body, key=f"CREATE:{qr.case_id}:{built.content_hash[:16]}")
    qid = doc["id"]
    _save_bases(ctx, qid, 1, built)
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE cases SET quotation_id=?, updated_at=? WHERE id=?", (qid, ctx.store.now(), qr.case_id))
    ctx.store.event(qr.case_id, "quote_drafted", qid, version=1, plan=built.chosen, flags=[f.code for f in built.flags])
    ctx.store.audit("system", None, "quote_created", "quotation", qid, version=1, hash=built.content_hash)
    return qid, 1


def revise(ctx: Ctx, qid: str, from_version: int, user: User, *, rep_discount_pct: Decimal | None = None,
           chosen: str | None = None, lines: dict[str, int] | None = None) -> int:
    """A new version from an existing one. Frozen versions are never edited."""
    base = load_version(ctx, qid, from_version)
    if lines is not None:
        if user.role != "rep" and not user.is_approver:
            raise QuoteError("only a rep or an approver can change quantities")
        if base["status"] in ("accepted", "declined", "expired"):
            raise QuoteError(f"v{from_version} is {base['status']}; quantities can no longer be changed on it. Cancel the order or start a new enquiry")
        if not lines or any(not isinstance(q, int) or isinstance(q, bool) or q <= 0 for q in lines.values()):
            raise QuoteError("give at least one product with a whole quantity above zero")
    qr = load_request(ctx, qid, from_version)
    changes = {"created_by": user.id}
    if rep_discount_pct is not None:
        if user.role != "rep" and not user.is_approver:
            raise QuoteError("only a rep or an approver can set a discount")
        changes["rep_discount_pct"] = rep_discount_pct
    if chosen is not None:
        changes["chosen"] = chosen
    if lines is not None:
        changes["lines"] = tuple(sorted(lines.items()))
        changes["chosen"] = "default"  # a plan chosen for the old quantities does not fit the new ones
    from dataclasses import replace
    built = build_version(ctx, replace(qr, **changes))
    doc = ctx.clients.erp.post(f"/quotations/{qid}/versions", json=_version_body(built, user.id, from_version),
                               key=f"VERSION:{qid}:{from_version}:{built.content_hash[:16]}")
    version = max(v["version"] for v in doc["versions"])
    _save_bases(ctx, qid, version, built)
    if base["status"] in ("draft", "pending_approval", "approved"):
        ctx.clients.erp.post(f"/quotations/{qid}/versions/{from_version}/transition", json={"to": "withdrawn", "actor": user.id,
                             "reason": "replaced by a new version"}, key=f"WITHDRAW:{qid}:{from_version}")
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE approvals SET status='changed', decided_by=?, decided_at=? WHERE quotation_id=? AND version=? AND status='pending'",
                         (user.id, ctx.store.now(), qid, from_version))
    ctx.store.event(qr.case_id, "quote_revised", qid, from_version=from_version, version=version, by=user.id)
    ctx.store.audit("user", user.id, "quote_revised", "quotation", qid, from_version=from_version, version=version)
    return version


# ---- submit and approve -------------------------------------------------------------------
def open_blocks(ctx: Ctx, qid: str, version: int) -> list[dict]:
    return ctx.store.db.query(
        "SELECT * FROM flags WHERE quotation_id=? AND version=? AND severity='block' AND resolved_at IS NULL", (qid, version))


def _flags_for_routing(ctx: Ctx, qid: str, version: int):
    from limitara.core.pricing.flags import Flag
    out = []
    for r in ctx.store.db.query("SELECT * FROM flags WHERE quotation_id=? AND version=?", (qid, version)):
        meta = json.loads(r["provenance_json"] or "{}")
        m = meta.get("margin_pct")
        out.append(Flag(r["code"], r["detail"], role=r["required_role"], margin_pct=Decimal(m) if m else None))
    return out


def submit(ctx: Ctx, qid: str, version: int, actor: str = "system") -> list[str]:
    ver = load_version(ctx, qid, version)
    if ver["status"] != "draft":
        raise QuoteError(f"only a draft can be submitted; this version is {ver['status']}")
    blocks = open_blocks(ctx, qid, version)
    if blocks:
        raise QuoteError("resolve these first: " + "; ".join(f"{b['code']}: {b['detail']}" for b in blocks))
    routing = route_approvals(_flags_for_routing(ctx, qid, version), authorities(ctx))
    if routing.unapprovable:
        raise QuoteError("no approver has the authority for this margin; reprice it. " + "; ".join(routing.reasons))
    ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "pending_approval", "actor": actor},
                         key=f"SUBMIT:{qid}:{version}")
    now = ctx.store.now()
    with ctx.store.db.tx() as conn:
        for role in routing.roles:
            conn.execute("INSERT INTO approvals(quotation_id, version, required_role, status, created_at) VALUES (?,?,?,?,?)",
                         (qid, version, role, "pending", now))
    for s in ctx.clients.crm.get("/staff").data:
        if s["role"] in routing.roles or (s["role"] == HEAD and MANAGER in routing.roles):
            ctx.store.alert(s["id"], "approval", f"{qid} v{version} is waiting for your approval", f"{qid}:{version}")
    case = load_request(ctx, qid, version).case_id
    ctx.store.event(case, "quote_submitted", qid, version=version, roles=list(routing.roles))
    ctx.store.audit("user" if actor != "system" else "system", actor, "quote_submitted", "quotation", qid, version=version, roles=list(routing.roles))
    return list(routing.roles)


def pending_approvals(ctx: Ctx, qid: str, version: int) -> list[dict]:
    return ctx.store.db.query("SELECT * FROM approvals WHERE quotation_id=? AND version=? AND status='pending'", (qid, version))


def approve(ctx: Ctx, qid: str, version: int, user: User, reason: str = "") -> str:
    if not user.is_approver:
        raise QuoteError("only an approver can approve a quote")
    ver = load_version(ctx, qid, version)
    if ver["status"] != "pending_approval":
        raise QuoteError(f"this version is {ver['status']}, not pending approval")
    if ver["created_by"] == user.id:
        raise QuoteError("you created this version, so you cannot approve it")
    blocks = open_blocks(ctx, qid, version)
    if blocks:
        raise QuoteError("blocking flags must be resolved first: " + ", ".join(b["code"] for b in blocks))
    slot = next((a for a in pending_approvals(ctx, qid, version) if can_satisfy(user.role, a["required_role"])), None)
    if slot is None:
        raise QuoteError(f"nothing is waiting for a {user.role}")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE approvals SET status='approved', decided_by=?, decided_at=?, reason=? WHERE id=?",
                     (user.id, ctx.store.now(), reason, slot["id"]))
    ctx.store.audit("user", user.id, "quote_approved", "quotation", qid, version=version, role=slot["required_role"], reason=reason)
    if pending_approvals(ctx, qid, version):
        return "waiting_for_more_approvals"
    ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "approved", "actor": user.id},
                         key=f"APPROVED:{qid}:{version}")
    return finalize_send(ctx, qid, version)


def reject(ctx: Ctx, qid: str, version: int, user: User, reason: str) -> None:
    if not user.is_approver:
        raise QuoteError("only an approver can reject a quote")
    if not reason.strip():
        raise QuoteError("a reason is required to reject")
    ver = load_version(ctx, qid, version)
    if ver["status"] != "pending_approval":
        raise QuoteError(f"this version is {ver['status']}, not pending approval")
    ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "rejected", "actor": user.id, "reason": reason},
                         key=f"REJECT:{qid}:{version}")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE approvals SET status='rejected', decided_by=?, decided_at=?, reason=? WHERE quotation_id=? AND version=? AND status='pending'",
                     (user.id, ctx.store.now(), reason, qid, version))
    case = load_request(ctx, qid, version).case_id
    ctx.store.task(case, "rep", "quote_rejected", f"{qid} v{version} was rejected by {user.name}: {reason}", quotation_id=qid, version=version)
    ctx.store.event(case, "quote_rejected", qid, version=version, reason=reason)
    ctx.store.audit("user", user.id, "quote_rejected", "quotation", qid, version=version, reason=reason)


def change(ctx: Ctx, qid: str, version: int, user: User, *, rep_discount_pct: Decimal | None = None, chosen: str | None = None) -> int:
    """An approver changes a quote: a new version (never an edit), which needs someone else's approval."""
    if not user.is_approver:
        raise QuoteError("only an approver can change a quote")
    new = revise(ctx, qid, version, user, rep_discount_pct=rep_discount_pct, chosen=chosen)
    try:
        submit(ctx, qid, new, actor="system")
    except QuoteError:
        pass  # blocking flags or no authority: the rep sees it as a task
    return new


# ---- hold and send ------------------------------------------------------------------------
def hold_requests(plan, bom):
    items: dict[tuple, int] = {}
    days: list[dict] = []
    for sh in plan.shipments:
        for a in sh.allocs:
            if a.kind == "stock":
                items[(a.location, a.product)] = items.get((a.location, a.product), 0) + a.qty
            elif a.kind == "plant_stock":
                items[(a.location, a.product)] = items.get((a.location, a.product), 0) + a.qty
            else:
                material, per = bom[a.product]
                items[(a.location, material)] = items.get((a.location, material), 0) + a.qty * per
                days.append({"line_id": a.line, "day": a.day.isoformat(), "product_id": a.product, "units": a.qty})
    return ([{"location_id": k[0], "item_id": k[1], "qty": q} for k, q in sorted(items.items())], days)


def release_holds(ctx: Ctx, ref: str) -> None:
    for res in ctx.clients.erp.get("/reservations", ref_id=ref).data:
        if res["status"] == "active":
            ctx.clients.erp.post(f"/reservations/{res['id']}/release", key=f"RELEASE:{res['id']}")
    for bk in ctx.clients.production.get("/bookings", ref_id=ref).data:
        if bk["status"] == "active":
            ctx.clients.production.post(f"/bookings/{bk['id']}/release", key=f"RELEASE:{bk['id']}")


def _replan(ctx: Ctx, qid: str, version: int, case_id: str, why: str) -> str:
    ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "needs_replan", "actor": "system", "reason": why},
                         key=f"REPLAN:{qid}:{version}")
    ctx.store.task(case_id, "rep", "replan", f"{qid} v{version} was approved but cannot be sent as it stands: {why}. Create a new version.",
                   quotation_id=qid, version=version)
    ctx.store.event(case_id, "needs_replan", qid, version=version, why=why)
    return "needs_replan"


def finalize_send(ctx: Ctx, qid: str, version: int) -> str:
    qr = load_request(ctx, qid, version)
    stored = load_plans(ctx, qid, version)
    ver = load_version(ctx, qid, version)
    doc = ctx.clients.erp.get(f"/quotations/{qid}").data
    previous = next((v["version"] for v in doc["versions"] if v["status"] == "sent"), None)
    owners = (f"{qid}:v{previous}",) if previous else ()
    try:
        snap = ctx.builder.build(owner_refs=owners)
    except Unknown as exc:
        return _replan(ctx, qid, version, qr.case_id, f"data was unavailable ({exc})")
    problems = verify_plan(stored["plan"], snap, qr.lines, qr.city, ctx.config)
    if problems:
        return _replan(ctx, qid, version, qr.case_id, "the plan no longer holds: " + "; ".join(problems[:3]))
    items, days = hold_requests(stored["plan"], snap.bom)
    ref = f"{qid}:v{version}"
    try:
        if items:
            ctx.clients.erp.post("/reservations", json={"ref_id": ref, "kind": "soft", "expires_at": ver["valid_until"], "items": items},
                                 key=f"HOLD:{ref}:erp")
        if days:
            ctx.clients.production.post("/bookings", json={"ref_id": ref, "kind": "soft", "expires_at": ver["valid_until"], "days": days},
                                        key=f"HOLD:{ref}:prod")
    except Rejected as exc:
        release_holds(ctx, ref)
        return _replan(ctx, qid, version, qr.case_id, f"holds could not be placed ({exc.code})")
    customer = ctx.clients.crm.get(f"/accounts/{qr.customer_id}").data
    from limitara.core.quotes.render import render_quote
    subject, body = render_quote(
        qid, version, customer["name"], ver["lines"],
        [{"units": s.units, "warehouse": s.warehouse, "ship_date": s.ship_date.isoformat(), "delivery_date": s.delivery.isoformat(),
          "freight": str(s.freight)} for s in stored["plan"].shipments],
        parse_instant(ver["valid_until"]), ver["interpretation_note"], ver["total_ex_tax"], list(stored["plan"].notes), stored["single"])
    oid = gate.draft(ctx, "quote", qr.contact_email, subject, body, case_id=qr.case_id, content_hash=ver["content_hash"],
                     refs={"quotation_id": qid, "version": version, "thread_id": qr.thread_id, "customer_id": qr.customer_id,
                           "in_reply_to": qr.in_reply_to, "verified_at": to_wire(ctx.clock.now())},
                     idempotency_key=f"QUOTE:{qid}:{version}")
    try:
        gate.release(ctx, oid)
    except gate.GateRefused as exc:
        release_holds(ctx, ref)
        return _replan(ctx, qid, version, qr.case_id, f"the Outbound Gate refused it: {exc}")
    ctx.clients.erp.post(f"/quotations/{qid}/versions/{version}/transition", json={"to": "sent", "actor": "system"},
                         key=f"SENT:{qid}:{version}")
    if previous:
        release_holds(ctx, f"{qid}:v{previous}")
    ctx.clients.crm.post(f"/accounts/{qr.customer_id}/activities", json={"kind": "quote", "summary": f"{qid} v{version} sent",
                         "by_staff": None}, key=f"CRM-SENT:{qid}:{version}")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE cases SET status='awaiting_customer', updated_at=? WHERE id=?", (ctx.store.now(), qr.case_id))
    ctx.store.event(qr.case_id, "quote_sent", qid, version=version, outbound=oid)
    ctx.store.audit("system", None, "quote_sent", "quotation", qid, version=version, outbound=oid)
    return "sent"


def record_consent(ctx: Ctx, qid: str, version: int, user: User) -> None:
    if user.role != "rep" and not user.is_approver:
        raise QuoteError("only staff can record a customer's consent")
    qr = load_request(ctx, qid, version)
    with ctx.store.db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO consents VALUES (?,?,?,?,?)", (qr.case_id, qid, version, user.id, ctx.store.now()))
        conn.execute("UPDATE flags SET resolved_by=?, resolved_at=? WHERE quotation_id=? AND version=? AND code='SPLIT_NEEDS_CONSENT'",
                     (user.id, ctx.store.now(), qid, version))
    ctx.store.event(qr.case_id, "consent_recorded", qid, version=version, by=user.id)
    ctx.store.audit("user", user.id, "split_consent_recorded", "quotation", qid, version=version)

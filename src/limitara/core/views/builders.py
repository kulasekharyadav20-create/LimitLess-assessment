"""What each audience may see. Every view is built ONLY from named fields (an allow-list), never by
removing secrets from a richer object, so a new internal field can never leak to a customer."""

import json

from limitara.clock import IST, parse_instant
from limitara.contracts import Unknown
from limitara.core.context import Ctx
from limitara.core.monitor import slips
from limitara.core.orders.stage import order_stage
from limitara.core.promise.serialize import plan_to_dict
from limitara.core.quotes import service as quotes
from limitara.core.users import User

SENT_STATES = ("sent", "accepted", "declined", "expired", "superseded")  # a customer has seen these versions
CUSTOMER_STAGE = {
    "confirmed": "Order confirmed", "in_production": "In production", "ready": "Ready to ship",
    "part_dispatched": "Part of your order has been dispatched", "dispatched": "Dispatched", "delivered": "Delivered",
}
BEING_UPDATED = "Being updated by your account manager"
DATE_UNDER_REVIEW = "Delivery date being confirmed by your account manager"


def _ist(wire: str | None) -> str | None:
    return parse_instant(wire).astimezone(IST).strftime("%d %b %Y %H:%M IST") if wire else None


def can_see_order(ctx: Ctx, user: User, meta: dict) -> bool:
    if user.role == "customer":
        return meta["customer_id"] == user.account_id
    if user.role == "rep":
        owner = ctx.clients.crm.get(f"/accounts/{meta['customer_id']}").data["owner_rep_id"]
        return owner == user.id
    return user.is_approver


def _facts(ctx: Ctx, meta: dict) -> dict:
    so = meta["order_id"]
    try:
        wos = ctx.clients.production.get("/work-orders", order_ref=so).data
        ships = ctx.clients.dispatch.get("/shipments", order_ref=so).data
    except Unknown:
        wos, ships = [], []
    return {"wos": wos, "ships": ships, "stage": order_stage(meta["state"], wos, ships), "at_risk": slips.slip_open(ctx, so)}


def build_customer_order(ctx: Ctx, meta: dict) -> dict:
    f = _facts(ctx, meta)
    hide_dates = f["at_risk"]
    shipments = [{"id": s["id"], "carrier": s["carrier"], "status": s["status"], "units": sum(l["units"] for l in s["lines"]),
                  "ship_date": None if hide_dates else s["ship_date"], "delivery_date": None if hide_dates else s["delivery_date"],
                  "freight": s["freight_charge"]} for s in f["ships"] if s["kind"] == "customer" and s["status"] != "cancelled"]
    doc = ctx.clients.erp.get(f"/quotations/{meta['quotation_id']}").data
    quotes_seen = [{"quotation": doc["id"], "version": v["version"], "status": v["status"], "valid_until": _ist(v["valid_until"]),
                    "total": v["total_ex_tax"], "accepted": v["status"] == "accepted",
                    "lines": [{"product": l["product_id"], "qty": l["qty"], "unit_price": l["unit_price"],
                               "delivery_by": None if hide_dates and v["status"] == "accepted" else l["promised_delivery_date"]} for l in v["lines"]]}
                   for v in doc["versions"] if v["status"] in SENT_STATES]
    events = [{"at": e["at"], "what": {"quote_sent": "Quotation sent", "order_confirmed": "Order confirmed"}[e["kind"]]}
              for e in ctx.store.db.query("SELECT * FROM case_events WHERE case_id=? AND kind IN ('quote_sent','order_confirmed') ORDER BY id", (meta["case_id"],))]
    return {"order": meta["order_id"], "stage": CUSTOMER_STAGE.get(f["stage"], BEING_UPDATED),
            "delivery_note": DATE_UNDER_REVIEW if hide_dates else None, "shipments": shipments,
            "quotations": quotes_seen, "timeline": events}


def rep_flag_text(f: dict) -> str:
    if f["code"] in ("BELOW_FLOOR", "TERMS_BREACH_FLOOR"):
        return "A line is below the margin floor. An approver will review it and see the numbers."
    return f["detail"]


def _flags(ctx: Ctx, qid: str, version: int, full: bool) -> list[dict]:
    out = []
    for f in ctx.store.db.query("SELECT * FROM flags WHERE quotation_id=? AND version=? ORDER BY id", (qid, version)):
        meta = json.loads(f["provenance_json"] or "{}")
        item = {"code": f["code"], "severity": f["severity"], "role": f["required_role"], "resolved": bool(f["resolved_at"]),
                "detail": f["detail"] if full else rep_flag_text(f)}
        if full:
            item["provenance"] = meta.get("prov", [])
            item["margin_pct"] = meta.get("margin_pct")
        out.append(item)
    return out


def _approvals(ctx: Ctx, qid: str, version: int) -> list[dict]:
    return [{"role": a["required_role"], "status": a["status"], "by": a["decided_by"], "reason": a["reason"]}
            for a in ctx.store.db.query("SELECT * FROM approvals WHERE quotation_id=? AND version=? ORDER BY id", (qid, version))]


def build_rep_order(ctx: Ctx, meta: dict) -> dict:
    f = _facts(ctx, meta)
    doc = ctx.clients.erp.get(f"/quotations/{meta['quotation_id']}").data
    versions = [{"version": v["version"], "status": v["status"], "valid_until": _ist(v["valid_until"]), "total": v["total_ex_tax"],
                 "lines": [{"product": l["product_id"], "qty": l["qty"], "list_price": l["list_price"], "discount_pct": l["discount_pct"],
                            "unit_price": l["unit_price"], "delivery_by": l["promised_delivery_date"]} for l in v["lines"]],
                 "flags": _flags(ctx, doc["id"], v["version"], False), "approvals": _approvals(ctx, doc["id"], v["version"])}
                for v in doc["versions"]]
    slip_rows = ctx.store.db.query("SELECT id, status, promised, forecast, work_order_id, options_json FROM slip_events WHERE order_id=? ORDER BY id", (meta["order_id"],))
    return {"order": meta["order_id"], "case": meta["case_id"], "customer": meta["customer_id"], "state": meta["state"], "stage": f["stage"],
            "at_risk": f["at_risk"], "versions": versions,
            "work_orders": [{k: w[k] for k in ("id", "product_id", "qty", "state", "promised_date", "forecast_finish", "note")} for w in f["wos"]],
            "shipments": [{k: s[k] for k in ("id", "kind", "from_location", "destination", "ship_date", "delivery_date", "status", "freight_charge")} for s in f["ships"]],
            "slips": [{"id": s["id"], "status": s["status"], "promised": s["promised"], "forecast": s["forecast"], "work_order": s["work_order_id"],
                       "options": [{"label": o["label"], "kind": o["kind"], "delivery": o.get("delivery"), "freight": o.get("freight")} for o in json.loads(s["options_json"] or "[]")]}
                      for s in slip_rows]}


def build_approver_quote(ctx: Ctx, qid: str, version: int) -> dict:
    ver = quotes.load_version(ctx, qid, version)
    stored = quotes.load_plans(ctx, qid, version)
    prices = [json.loads(r["basis_json"]) for r in ctx.store.db.query("SELECT basis_json FROM price_bases WHERE quotation_id=? AND version=? ORDER BY line_no", (qid, version))]
    prov = json.loads(ctx.store.db.one("SELECT provenance_json FROM plan_bases WHERE quotation_id=? AND version=?", (qid, version))["provenance_json"])
    qr = quotes.load_request(ctx, qid, version)
    best = stored["single"]
    return {"quotation": qid, "version": version, "status": ver["status"], "customer": ver["customer_id"], "created_by": ver["created_by"],
            "valid_until": _ist(ver["valid_until"]), "requested_by": ver["requested_delivery_date"], "interpretation": ver["interpretation_note"],
            "total": ver["total_ex_tax"], "prices": prices, "flags": _flags(ctx, qid, version, True), "approvals": _approvals(ctx, qid, version),
            "plan": plan_to_dict(stored["plan"]), "plan_choice": stored["chosen"], "choices": sorted(stored["plans"]),
            "single_shipment_comparison": best, "basis": prov, "customer_requests": list(qr.customer_requests)}


def approval_queue(ctx: Ctx, user: User) -> list[dict]:
    from limitara.core.approvals.router import can_satisfy
    out = []
    for a in ctx.store.db.query("SELECT * FROM approvals WHERE status='pending' ORDER BY id"):
        if can_satisfy(user.role, a["required_role"]):
            ver = quotes.load_version(ctx, a["quotation_id"], a["version"])
            if ver["status"] == "pending_approval":
                out.append({"quotation": a["quotation_id"], "version": a["version"], "role": a["required_role"], "customer": ver["customer_id"],
                            "total": ver["total_ex_tax"], "created_by": ver["created_by"],
                            "flags": [f["code"] for f in _flags(ctx, a["quotation_id"], a["version"], False)]})
    return out

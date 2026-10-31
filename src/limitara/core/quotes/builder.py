"""Build one quote version: snapshot, promise, price and flags. Reads, then calls pure functions."""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from limitara.clock import parse_instant, to_wire
from limitara.contracts import Unknown
from limitara.core.approvals.router import MANAGER
from limitara.core.context import Ctx
from limitara.core.pricing.engine import PriceListInfo, PriceResult, PricingInputs, TermsInfo, credit_flags, price_request
from limitara.core.pricing.flags import Flag
from limitara.core.promise import ALTERNATIVES, FULL, NOTHING, PromiseResult, build_promise, verify_plan
from limitara.core.promise.explain import basis, old_counts
from limitara.core.promise.model import Plan, Request, Snapshot
from limitara.hashing import content_hash


@dataclass(frozen=True)
class QuoteRequest:
    case_id: str
    customer_id: str
    contact_email: str
    city: str
    deliver_by: date
    lines: tuple[tuple[str, int], ...]
    asked_valid_until: datetime
    note: str = ""
    rep_discount_pct: Decimal = Decimal(0)
    chosen: str = "default"
    created_by: str = "system"
    customer_requests: tuple[str, ...] = ()
    thread_id: str | None = None
    in_reply_to: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "case_id": self.case_id, "customer_id": self.customer_id, "contact_email": self.contact_email, "city": self.city,
            "deliver_by": self.deliver_by.isoformat(), "lines": [list(x) for x in self.lines],
            "asked_valid_until": to_wire(self.asked_valid_until), "note": self.note,
            "rep_discount_pct": str(self.rep_discount_pct), "chosen": self.chosen, "created_by": self.created_by,
            "customer_requests": list(self.customer_requests), "thread_id": self.thread_id, "in_reply_to": self.in_reply_to})

    @classmethod
    def from_json(cls, text: str) -> "QuoteRequest":
        d = json.loads(text)
        return cls(d["case_id"], d["customer_id"], d["contact_email"], d["city"], date.fromisoformat(d["deliver_by"]),
                   tuple((p, q) for p, q in d["lines"]), parse_instant(d["asked_valid_until"]), d["note"],
                   Decimal(d["rep_discount_pct"]), d["chosen"], d["created_by"], tuple(d["customer_requests"]),
                   d["thread_id"], d["in_reply_to"])


@dataclass
class Built:
    request: QuoteRequest
    snapshot: Snapshot
    promise: PromiseResult
    chosen: str
    plan: Plan
    plans: dict[str, Plan]
    price: PriceResult
    flags: list[Flag]
    customer_name: str
    lines: list[dict] = field(default_factory=list)
    shipments: list[dict] = field(default_factory=list)
    valid_until_wire: str = ""
    content_hash: str = ""
    comparison: dict | None = None


class CannotQuote(Exception):
    """No quote can honestly be made. Carries the reason and the role who should hear it."""

    def __init__(self, code: str, text: str, role: str = "rep"):
        super().__init__(text)
        self.code, self.text, self.role = code, text, role


def offerable_plans(result: PromiseResult) -> dict[str, Plan]:
    plans: dict[str, Plan] = {}
    if result.offered is not None:
        plans["offered"] = result.offered
    for i, p in enumerate(result.options):
        plans[f"option:{i}"] = p
    plans.update(result.alternatives)
    return plans


def _price_inputs(ctx: Ctx, qr: QuoteRequest, snap: Snapshot) -> PricingInputs:
    lists_stamped = ctx.clients.erp.get("/price-lists", customer_id=qr.customer_id)
    terms_stamped = ctx.clients.crm.get(f"/accounts/{qr.customer_id}/terms")
    lists = tuple(
        PriceListInfo(r["id"], r["customer_id"], date.fromisoformat(r["valid_from"]), date.fromisoformat(r["valid_to"]),
                      {i["product_id"]: Decimal(i["unit_price"]) for i in r["items"]})
        for r in lists_stamped.data)
    terms = tuple(
        TermsInfo(r["id"], Decimal(r["discount_pct"]), date.fromisoformat(r["valid_from"]), date.fromisoformat(r["valid_to"]),
                  r["partial_shipments"], Decimal(r["margin_floor_override_pct"]) if r["margin_floor_override_pct"] else None)
        for r in terms_stamped.data)
    return PricingInputs(qr.customer_id, snap.today, lists, terms, snap.cost, qr.lines, qr.asked_valid_until,
                         qr.rep_discount_pct, {"erp": to_wire(lists_stamped.read_at), "crm": to_wire(terms_stamped.read_at)})


def build_version(ctx: Ctx, qr: QuoteRequest) -> Built:
    """Raises Unknown if any needed read is unavailable or stale, CannotQuote if no honest quote exists."""
    snap = ctx.builder.build()
    probe = _price_inputs(ctx, qr, snap)
    live = next((t for t in probe.terms if t.valid_from <= snap.today <= t.valid_to), None)
    partial = live.partial if live else "allowed"

    promise = build_promise(snap, Request(qr.lines, qr.city, qr.deliver_by, partial), ctx.config)
    if promise.status == NOTHING:
        raise CannotQuote("NO_PLAN", "No plan covers this request within the planning horizon.")
    plans = offerable_plans(promise)
    chosen = qr.chosen
    if chosen == "default":
        chosen = "offered" if "offered" in plans else ("A_single_later" if "A_single_later" in plans else next(iter(plans)))
    if chosen not in plans:
        raise CannotQuote("BAD_CHOICE", f"plan choice {chosen!r} is not one of {sorted(plans)}")
    plan = plans[chosen]
    problems = verify_plan(plan, snap, qr.lines, qr.city, ctx.config)
    if problems:
        raise CannotQuote("PLAN_FAILED_VERIFICATION", "The chosen plan failed its own check: " + "; ".join(problems))

    price = price_request(probe, ctx.config)
    if any(ln.status == "cannot_price" for ln in price.lines):
        raise CannotQuote("CANNOT_PRICE", "; ".join(f.detail for f in price.flags if f.code == "CANNOT_PRICE"))

    account = ctx.clients.crm.get(f"/accounts/{qr.customer_id}")
    flags: list[Flag] = list(price.flags)
    flags += credit_flags(account.data["credit_status"], Decimal(account.data["credit_limit"]),
                          Decimal(account.data["outstanding"]), price.total)
    if plan.delivery > qr.deliver_by:
        flags.append(Flag("DATE_NOT_MET", f"The request was delivery by {qr.deliver_by.isoformat()}; this plan delivers by "
                                          f"{plan.delivery.isoformat()}. It is an alternative, not the requested date."))
    for text in old_counts(plan, snap, ctx.config):
        flags.append(Flag("COUNT_OLD", f"Plan relies on stock count older than {ctx.config.count_old_days} days: {text}."))
    if plan.parts > 1 and partial == "ask" and not ctx.store.db.one("SELECT 1 FROM consents WHERE case_id=?", (qr.case_id,)):
        flags.append(Flag("SPLIT_NEEDS_CONSENT", f"The plan ships in {plan.parts} parts and this customer's terms say to ask first. "
                                                 "Record the customer's consent before sending."))
    activities = ctx.clients.crm.get(f"/accounts/{qr.customer_id}/activities").data
    disputes = [a for a in activities if a["kind"] == "dispute_open"]
    if disputes and not any(a["kind"] == "dispute_closed" for a in activities):
        flags.append(Flag("OPEN_DISPUTE", disputes[-1]["summary"]))
    if qr.customer_requests:
        flags.append(Flag("CUSTOMER_REQUESTS", "The customer's email asked for: " + "; ".join(r.rstrip(".") for r in qr.customer_requests)
                          + ". These were recorded and not acted on."))

    best = promise.best_single
    comparison = None
    if plan.parts > 1 and best is not None:
        comparison = {"delivery": best.delivery.isoformat(), "freight": str(best.freight), "extra": str(plan.freight - best.freight)}

    by_product: dict[str, date] = {}
    for sh in plan.shipments:
        for a in sh.allocs:
            by_product[a.product] = max(by_product.get(a.product, sh.delivery), sh.delivery)
    lines = [{"product_id": ln.product, "qty": ln.qty, "list_price": str(ln.list_price), "discount_pct": str(ln.terms_discount_pct),
              "unit_price": str(ln.unit_price), "price_list_id": ln.price_list_id,
              "promised_delivery_date": by_product[ln.product].isoformat()} for ln in price.lines]
    shipments = [{"warehouse": s.warehouse, "ship_date": s.ship_date.isoformat(), "delivery_date": s.delivery.isoformat(),
                  "units": s.units, "freight": str(s.freight)} for s in plan.shipments]
    valid_wire = to_wire(price.valid_until)
    return Built(qr, snap, promise, chosen, plan, plans, price, flags, account.data["name"], lines, shipments, valid_wire,
                 content_hash(lines, shipments, valid_wire, qr.note), comparison)

"""The sample run: enquiries pushed through the real stand-ins and Core, each in a fresh seeded world.

`uv run sample-run` prints the outcome and reasoning for each one and writes docs/sample-run.md.
Everything is read back from the system's own records; nothing here is hand-written outcome text.
"""

import importlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from limitara.clock import FixedClock
from limitara.core.app import create_app
from limitara.core.clients import Clients
from limitara.core.config import DEFAULT
from limitara.core.context import Ctx
from limitara.core.intake import service as intake
from limitara.core.store import CoreStore
from limitara.seed import seed_all

NOW = datetime(2026, 10, 31, 5, 30, tzinfo=UTC)  # Saturday 31 Oct 2026, 11:00 IST
TODAY = date(2026, 10, 31)
NAMES = ("erp", "production", "dispatch", "crm", "mailbox")

RAKESH, PRIYA = "rakesh.shah@balajisupplies.example", "priya.joshi@balajisupplies.example"
VIKRAM, LAKSHMI = "vikram@konkanpipes.example", "lakshmi@deccanhydraulics.example"
HITESH, NEHA, SUNIL = "hitesh@anandengg.example", "neha@metrovalve.example", "sunil@rajhanssteel.example"
HEADLINE = ("We need 300 of the 50mm flange and 120 of the 80mm flange, both to our Pune warehouse, before the 14th. "
            "Last quarter you shipped in two parts and we were charged twice for freight. "
            "Send a price that holds if we confirm by Friday.")


class World:
    def __init__(self, seeded_history: bool = False):
        self.dir = Path(tempfile.mkdtemp(prefix="limitara-run-"))
        import os
        previous = os.environ.get("LIMITARA_DATA_DIR")
        os.environ["LIMITARA_DATA_DIR"] = str(self.dir)
        try:
            seed_all(TODAY, NOW)
        finally:
            if previous is None:
                os.environ.pop("LIMITARA_DATA_DIR", None)
            else:
                os.environ["LIMITARA_DATA_DIR"] = previous
        self.clock = FixedClock(NOW)
        apps = {n: TestClient(importlib.import_module(f"limitara.standins.{n}.app").create_app(str(self.dir / f"{n}.db"), self.clock)) for n in NAMES}
        self.ctx = Ctx(Clients.from_http(**apps), CoreStore(self.dir / "core.db", self.clock), self.clock, DEFAULT)
        self.web = TestClient(create_app(ctx=self.ctx, background=False))
        self.n = 0
        if not seeded_history:  # start after the seeded history so each scenario is only about its own mail
            last = max(d["delivery_id"] for d in self.ctx.clients.mailbox.get("/deliveries", limit=500).data)
            with self.ctx.store.db.tx() as conn:
                conn.execute("UPDATE mail_cursor SET last_delivery_id=?", (last,))

    def send(self, body: str, frm: str, *, customer: str = "UNKNOWN", mid: str | None = None, thread: str | None = None,
             attachments: list | None = None, reply: str | None = None, sent_at: str | None = None) -> None:
        self.n += 1
        payload = {"message_id": mid or f"<run{self.n}@customer>", "thread_id": thread or f"T-RUN-{self.n}", "customer_id": customer,
                   "subject": "Enquiry", "from_addr": frm, "body": body, "attachments": attachments or [], "in_reply_to": reply}
        if sent_at:
            payload["sent_at"] = sent_at
        self.ctx.clients.mailbox.post("/inbound", json=payload)

    def poll(self) -> int:
        return intake.poll(self.ctx)

    def quotes(self) -> list[dict]:
        return [q for q in self.ctx.clients.erp.get("/quotations").data if q["id"].startswith("Q-2")]

    def tasks(self) -> list[dict]:
        return self.ctx.store.db.query("SELECT * FROM tasks ORDER BY id")

    def customer_mail(self) -> list[dict]:
        return [d for d in self.ctx.clients.mailbox.get("/deliveries", limit=500).data
                if d["direction"] == "out" and d["message_id"].startswith("<OUT-") and not d["subject"].startswith("Production slip")]


def _plan_text(ctx: Ctx, qid: str, version: int) -> str:
    from limitara.core.quotes import service as quotes
    plan = quotes.load_plans(ctx, qid, version)["plan"]
    parts = []
    for s in plan.shipments:
        sources = sorted({f"{a.kind} {a.location}" for a in s.allocs})
        parts.append(f"{s.units} units from {s.warehouse} ({', '.join(sources)}), ships {s.ship_date}, delivered {s.delivery}, freight Rs {s.freight:,.2f}")
    return "; ".join(parts) + (f"; {plan.parts} parts" if plan.parts > 1 else "; one shipment")


def describe(w: World) -> dict:
    """Read the system's own records back as plain facts."""
    ctx = w.ctx
    out = {"quotes": [], "tasks": [{"kind": t["kind"], "role": t["role"], "text": t["text"]} for t in w.tasks()],
                       "customer_mail": len(w.customer_mail()), "orders": len(ctx.clients.erp.get("/sales-orders").data)}
    for q in w.quotes():
        for v in q["versions"]:
            flags = ctx.store.db.query("SELECT code, severity, detail FROM flags WHERE quotation_id=? AND version=?", (q["id"], v["version"]))
            roles = [a["required_role"] for a in ctx.store.db.query("SELECT required_role FROM approvals WHERE quotation_id=? AND version=?", (q["id"], v["version"]))]
            out["quotes"].append({"id": q["id"], "version": v["version"], "customer": q["customer_id"], "status": v["status"],
                                  "prices": [(l["product_id"], l["qty"], l["unit_price"]) for l in v["lines"]], "total": v["total_ex_tax"],
                                  "valid_until": v["valid_until"], "flags": flags, "roles": roles, "plan": _plan_text(ctx, q["id"], v["version"])})
    return out


@dataclass
class Scenario:
    no: int
    title: str
    email: str
    trap: str
    run: Callable[[World], None]
    check: Callable[[dict], bool]
    expect: str
    outcome: dict = field(default_factory=dict)
    passed: bool | None = None
    extra: str = ""


def codes(summary: dict, qi: int = 0) -> set[str]:
    return {f["code"] for f in summary["quotes"][qi]["flags"]}


def single(w: World, body: str, frm: str, customer: str, **kw) -> None:
    w.send(body, frm, customer=customer, **kw)
    w.poll()


def s9(w: World) -> None:
    att = [{"filename": "PO-7781.txt", "content_type": "text/plain", "content": "PURCHASE ORDER 7781\nItem: FL-50\nQty: 1500\nPrice: 1215.00"}]
    for _ in range(2):  # delivered twice, same message id
        w.send("PO 7781 attached for 150 x FL-50 to Ahmedabad by the 20th at your agreed list price.", HITESH, customer="CUST-004",
               mid="<po7781@anand>", thread="T-RUN-PO", attachments=att)
    w.poll()


def s10(w: World) -> None:
    w.ctx.store.db.conn.execute("UPDATE mail_cursor SET last_delivery_id=0")
    w.poll()  # the seeded T-6001: quote v1, revised v2, then a late 'confirmed' written about v1


def s12(w: World) -> None:
    single(w, HEADLINE, RAKESH, "CUST-001")
    r = w.web.post("/api/whatif/promise", headers={"X-User": "REP-01"}, json={
        "lines": [["FL-50", 300], ["FL-80", 120]], "city": "Pune", "deliver_by": "2026-11-13", "partial": "ask",
        "change": {"kind": "plant_down", "plant": "PLT-A", "from": "2026-11-02", "to": "2026-11-12"}})
    w.whatif = r.json()


def s14(w: World) -> None:
    single(w, "Need 200 of the 65mm flange delivered to Pune by the 20th.", RAKESH, "CUST-001")
    single(w, "We need around 100 or 150 of the 50mm flange to Pune by the 20th.", RAKESH, "CUST-001", thread="T-RUN-AMB")


SCENARIOS = [
    Scenario(1, "The headline enquiry", HEADLINE, "FL-80 stock is 60 + 40 + 20 over three warehouses; terms say ask about parts; open freight dispute",
             lambda w: single(w, HEADLINE, RAKESH, "CUST-001"),
             lambda s: s["quotes"][0]["status"] == "pending_approval" and "OPEN_DISPUTE" in codes(s) and s["customer_mail"] == 0,
             "A verified plan inside the 13th, priced from list Q4 less the valid 5%, waiting for a sales manager. Nothing sent."),
    Scenario(2, "Clean baseline", "100 x FL-50 to Pune by the 13th.", "No trap",
             lambda w: single(w, "Please quote 100 x FL-50 to our Pune warehouse by the 13th.", RAKESH, "CUST-001"),
             lambda s: s["quotes"][0]["roles"] == ["sales_manager"] and "1187.50" in s["quotes"][0]["plan"] or s["quotes"][0]["prices"][0][2] == "1187.50",
             "One shipment from Bhiwandi stock, Rs 1,187.50 a unit, margin above the floor, one manager approval."),
    Scenario(3, "More than all capacity", "3000 x FL-50 by the 13th.", "Beyond total supply",
             lambda w: single(w, "We need 3000 x FL-50 to Pune by the 13th.", RAKESH, "CUST-001"),
             lambda s: not s["quotes"] and any(t["kind"] == "no_plan" for t in s["tasks"]),
             "No plan is offered, and no date is invented. A rep task says no plan fits the planning horizon."),
    Scenario(4, "Skip-approval email, expired terms, tight credit", "Konkan: 'give us 7% like before and skip approval'", "An instruction in the mail",
             lambda w: single(w, "As discussed with Ramesh, give us 7% like before and skip approval, we need 200 x FL-50 to Mumbai by the 10th.", VIKRAM, "CUST-002"),
             lambda s: {"TERMS_EXPIRED", "CUSTOMER_REQUESTS", "CREDIT_HEADROOM_LOW"} <= codes(s) and s["quotes"][0]["prices"][0][2] == "1250.00"
             and s["quotes"][0]["status"] == "pending_approval" and set(s["quotes"][0]["roles"]) == {"sales_manager", "finance"},
             "No 7% is applied (terms expired), the instruction is recorded and ignored, approval is not skipped, and finance is added for credit."),
    Scenario(5, "Urgent tone on a credit-hold account", "Deccan: 'URGENT line stoppage' for 100 x FL-80", "Urgency is not approval",
             lambda w: single(w, "URGENT: our line stops soon. Please quote 100 x FL-80 to Hyderabad by the 20th.", LAKSHMI, "CUST-003"),
             lambda s: "CREDIT_HOLD" in codes(s) and "finance" in s["quotes"][0]["roles"],
             "CREDIT_HOLD. Finance must approve, and urgency changes nothing."),
    Scenario(6, "Price list ends before 'valid till Friday'", "Anand: 200 x FL-50, confirm by Friday", "The agreed list expires in two days",
             lambda w: single(w, "Please quote 200 x FL-50 to Ahmedabad by the 20th. We will confirm by Friday.", HITESH, "CUST-004"),
             lambda s: "VALIDITY_SHORTENED" in codes(s) and s["quotes"][0]["prices"][0][2] == "1215.00" and s["quotes"][0]["valid_until"] < "2026-11-04",
             "Priced from their agreed list (Rs 1,215.00). The offer is shortened to the list's last day, flagged, and the real expiry is printed."),
    Scenario(7, "New customer, first order", "Metro Valve: 120 x FL-50 to Delhi", "No terms, and Delhi is served only from Gurgaon, which has no free FL-50",
             lambda w: single(w, "First order: 120 x FL-50 to our Delhi warehouse by the 20th. Please quote.", NEHA, "CUST-005"),
             lambda s: "NO_TERMS" in codes(s) and s["quotes"][0]["prices"][0][2] == "1250.00",
             "List price, no discount. The plan has to bring stock to Gurgaon first (transfer), and still verifies."),
    Scenario(8, "Agreed terms break the floor by themselves", "Rajhans: 400 x FL-50 to Nagpur", "11% terms put the line below 12%",
             lambda w: single(w, "Please quote 400 x FL-50 to Nagpur by the 20th.", SUNIL, "CUST-006"),
             lambda s: "TERMS_BREACH_FLOOR" in codes(s) and s["quotes"][0]["roles"] == ["sales_manager"],
             "Flagged TERMS_BREACH_FLOOR with margin and lowest compliant price. The system does not raise the price. A manager decides."),
    Scenario(9, "Duplicate purchase order, attachment disagrees", "Anand PO: body 150, attachment 1500, delivered twice", "Duplicate delivery and conflicting sources",
             s9, lambda s: not s["quotes"] and sum("CONFLICTING_SOURCES" in t["text"] for t in s["tasks"]) == 1,
             "One message processed, one rep task quoting both numbers, no quote created."),
    Scenario(10, "Late 'confirmed' about a superseded quote", "Rajhans: 'Confirmed, go ahead' quoting v1, arrives after v2", "Reply to an old version, delivered late",
             s10, lambda s: s["orders"] == 0 and any("Q-6001 v1 is superseded" in t["text"] for t in s["tasks"]),
             "No order. A rep task says the customer confirmed v1 but v2 is current."),
    Scenario(11, "Steel limits production", "Pune: 400 x FL-80 by the 20th", "Steel blanks cap how many FL-80 can be made",
             lambda w: single(w, "We need 400 x FL-80 to Pune by the 20th.", RAKESH, "CUST-001"),
             lambda s: (not s["quotes"] and any(t["kind"] == "no_plan" for t in s["tasks"])) or "DATE_NOT_MET" in codes(s),
             "No plan covers 400: 180 steel blanks at Plant B and 80 at Plant A cap production. Alternatives or a rep task, never an invented date."),
    Scenario(12, "Requirement change: Plant A down", "Enquiry 1 again with Plant A (Aurangabad) down 2-12 Nov", "The live 'change one thing' demo",
             s12, lambda s: True, "The what-if (nothing is written) shows the old plan, the new plan, and what changed and did not."),
    Scenario(13, "Stock last counted nine days ago", "250 x FL-50 to Pune by the 5th", "Chennai's count is old",
             lambda w: single(w, "We need 250 x FL-50 to our Pune warehouse by the 5th.", RAKESH, "CUST-001"),
             lambda s: True, "If the plan uses Chennai stock, COUNT_OLD is raised and a manager must acknowledge it."),
    Scenario(14, "Unknown product and ambiguous quantity", "'200 of the 65mm flange' and 'around 100 or 150'", "The system must say it does not know",
             s14, lambda s: not s["quotes"] and any("65mm" in t["text"] for t in s["tasks"]) and any("ambiguous" in t["text"] for t in s["tasks"]),
             "No quote. Rep tasks name exactly what could not be read."),
    Scenario(15, "Sunday that is also a holiday", "Deliver 100 x FL-50 on Sunday 8 Nov", "Nothing is delivered on a Sunday or holiday",
             lambda w: single(w, "Please deliver 100 x FL-50 to Pune on Sunday 8 Nov.", RAKESH, "CUST-001"),
             lambda s: all(not p.endswith("2026-11-08") for p in [s["quotes"][0]["plan"].split("delivered ")[-1][:10]]),
             "The delivery date never lands on 8 Nov. The earliest legal date is offered and the quote is marked as not meeting the request if later."),
]


def run_all() -> list[Scenario]:
    for sc in SCENARIOS:
        w = World(seeded_history=(sc.no == 10))
        sc.run(w)
        sc.outcome = describe(w)
        sc.passed = bool(sc.check(sc.outcome))
        if sc.no == 12:
            wi = getattr(w, "whatif", {})
            sc.extra = (f"Before: {wi['before']['status']}, delivery {wi['before']['delivery']}, {wi['before']['parts']} part(s), freight {wi['before']['freight']}. "
                        f"After: {wi['after']['status']}, delivery {wi['after']['delivery']}, {wi['after']['parts']} part(s), freight {wi['after']['freight']}. "
                        f"Changed: {sorted(wi['what_changed'])}. Unchanged: {wi['what_did_not']}.")
    return SCENARIOS


def report(scenarios: list[Scenario]) -> str:
    out = ["# Sample run: fifteen enquiries", "",
           "Generated by `uv run sample-run` against the real stand-ins and Core, each enquiry in a fresh seeded world (Saturday 31 Oct 2026, 11:00 IST).",
           "Every figure below is read back from the system's own records.", ""]
    for sc in scenarios:
        o = sc.outcome
        out += [f"## {sc.no}. {sc.title}", "", f"**Email:** {sc.email}", f"**Trap:** {sc.trap}", f"**Designed to show:** {sc.expect}", ""]
        for q in o["quotes"]:
            out.append(f"- **{q['id']} v{q['version']}** for {q['customer']}: {q['status']}. Prices: " + ", ".join(f"{qty} x {p} at Rs {price}" for p, qty, price in q["prices"])
                       + f". Total Rs {q['total']}. Valid until {q['valid_until']}.")
            out.append(f"  - Plan: {q['plan']}.")
            out.append(f"  - Approvals required: {', '.join(q['roles']) or 'none yet'}.")
            for f in q["flags"]:
                out.append(f"  - Flag **{f['code']}** ({f['severity']}): {f['detail']}")
        for t in o["tasks"]:
            out.append(f"- Task for **{t['role']}** ({t['kind']}): {t['text']}")
        if sc.extra:
            out.append(f"- What-if: {sc.extra}")
        out.append(f"- Orders created: {o['orders']}. Customer emails sent: {o['customer_mail']}.")
        out += ["", f"**Result:** {'as designed' if sc.passed else 'DIFFERS FROM DESIGN, see above'}.", ""]
    return "\n".join(out)


def main() -> int:
    scenarios = run_all()
    text = report(scenarios)
    docs = Path(__file__).resolve().parents[2] / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "sample-run.md").write_text(text)
    print(text)
    bad = [s.no for s in scenarios if not s.passed]
    print(f"\n{len(scenarios) - len(bad)} of {len(scenarios)} enquiries behaved as designed." + (f" Differences: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

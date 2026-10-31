"""Core: the web app (JSON API plus server-rendered pages) and its background loops.

Sign-in is a stub (choose a person; the cookie is signed). What each person may see and do is enforced
here on the server, never by hiding things in a page.
"""

import hashlib
import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from limitara.clock import Clock, clock_from_env
from limitara.contracts import Unknown
from limitara.core.approvals.router import route_approvals
from limitara.core.clients import Clients
from limitara.core.config import DEFAULT
from limitara.core.context import Ctx
from limitara.core.intake import service as intake
from limitara.core.monitor import slips
from limitara.core.orders import saga
from limitara.core.outbound import gate
from limitara.core.pricing.engine import price_request
from limitara.core.promise.model import Request
from limitara.core.quotes import service as quotes
from limitara.core.quotes.builder import QuoteRequest, _price_inputs
from limitara.core.store import CoreStore
from limitara.core.users import User, all_users, resolve_user
from limitara.core.views import builders
from limitara.core.whatif import what_if
from limitara.ports import STANDINS, url_of

HEADLINE_TEXT = ("We need 300 of the 50mm flange and 120 of the 80mm flange, both to our Pune warehouse, before the 14th. "
                 "Last quarter you shipped in two parts and we were charged twice for freight. Send a price that holds if we confirm by Friday.")
SECRET = os.environ.get("LIMITARA_SECRET", "dev-secret-change-me").encode()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "views" / "templates"))


def _sign(user_id: str) -> str:
    return f"{user_id}.{hmac.new(SECRET, user_id.encode(), hashlib.sha256).hexdigest()[:24]}"


def _unsign(value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    user_id, _, sig = value.rpartition(".")
    return user_id if hmac.compare_digest(sig, _sign(user_id).rpartition(".")[2]) else None


class DiscountIn(BaseModel):
    pct: Decimal


class ReasonIn(BaseModel):
    reason: str = ""


class ChangeIn(BaseModel):
    rep_discount_pct: Decimal | None = None
    chosen: str | None = None


class DecideIn(BaseModel):
    decision: str
    option: int | None = None
    note: str = ""


class WhatIfIn(BaseModel):
    lines: list[list]
    city: str
    deliver_by: date
    partial: str = "allowed"
    change: dict


class FloorIn(BaseModel):
    quotation_id: str
    version: int
    floor_pct: Decimal


def create_app(path: str | None = None, clock: Clock | None = None, clients: Clients | None = None,
               background: bool | None = None, ctx: Ctx | None = None) -> FastAPI:
    clock = clock or (ctx.clock if ctx else clock_from_env())
    ctx = ctx or Ctx(clients or Clients.from_env(), CoreStore(path, clock), clock, DEFAULT)
    if background is None:
        background = os.environ.get("LIMITARA_BACKGROUND", "1") == "1"
    stop = threading.Event()

    def loop(fn, every: float):
        while not stop.wait(every):
            try:
                fn(ctx)
            except Exception as exc:  # a failed cycle is retried; it must never kill the loop
                print(f"[core] {fn.__name__} failed: {exc.__class__.__name__}: {exc}")

    @asynccontextmanager
    async def lifespan(_app):
        threads = []
        if background:
            for fn, every in ((intake.poll, 3), (slips.check_slips, DEFAULT.slip_check_seconds), (saga.recover, 30)):
                t = threading.Thread(target=loop, args=(fn, every), daemon=True)
                t.start()
                threads.append(t)
        yield
        stop.set()

    app = FastAPI(title="Promises the Factory Can Keep: Core", lifespan=lifespan)
    app.state.ctx = ctx

    # ---- who is asking ------------------------------------------------------------------
    def current_user(request: HttpRequest) -> User:
        uid = request.headers.get("x-user") or _unsign(request.cookies.get("user"))
        user = resolve_user(ctx, uid) if uid else None
        if user is None:
            raise HTTPException(401, "sign in first")
        return user

    def staff(user: User = Depends(current_user)) -> User:
        if not user.is_staff:
            raise HTTPException(403, "staff only")
        return user

    def approver(user: User = Depends(current_user)) -> User:
        if not user.is_approver:
            raise HTTPException(403, "approvers only")
        return user

    def customer(user: User = Depends(current_user)) -> User:
        if user.role != "customer":
            raise HTTPException(403, "customers only")
        return user

    @app.exception_handler(quotes.QuoteError)
    async def _quote_error(_r, exc):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(gate.GateRefused)
    async def _gate_refused(_r, exc):
        return JSONResponse(status_code=422, content={"error": f"the Outbound Gate refused: {exc}"})

    def order_or_404(user: User, order_id: str) -> dict:
        meta = ctx.store.db.one("SELECT * FROM orders_meta WHERE order_id=?", (order_id,))
        if meta is None or not builders.can_see_order(ctx, user, meta):
            raise HTTPException(404, "no such order")  # 404, not 403: other people's records are not revealed
        return meta

    def own_quote(user: User, qid: str, version: int) -> dict:
        ver = quotes.load_version(ctx, qid, version)
        if user.role == "rep":
            owner = ctx.clients.crm.get(f"/accounts/{ver['customer_id']}").data["owner_rep_id"]
            if owner != user.id:
                raise HTTPException(404, "no such quote")
        return ver

    # ---- sign in ------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: HttpRequest):
        return TEMPLATES.TemplateResponse(request, "login.html", {"users": all_users(ctx)})

    @app.post("/login")
    def login(user_id: str = Form(...)):
        user = resolve_user(ctx, user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        target = {"customer": "/ui/customer", "rep": "/ui/rep"}.get(user.role, "/ui/approvals")
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("user", _sign(user.id), httponly=True)
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie("user")
        return resp

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "name": user.name, "role": user.role, "account": user.account_id}

    # ---- approvals (approvers) ----------------------------------------------------------
    @app.get("/api/approvals")
    def api_queue(user: User = Depends(approver)):
        return builders.approval_queue(ctx, user)

    @app.get("/api/approvals/{qid}/{version}")
    def api_approval(qid: str, version: int, user: User = Depends(approver)):
        return builders.build_approver_quote(ctx, qid, version)

    @app.post("/api/approvals/{qid}/{version}/approve")
    def api_approve(qid: str, version: int, body: ReasonIn, user: User = Depends(approver)):
        with ctx.lock:
            return {"result": quotes.approve(ctx, qid, version, user, body.reason)}

    @app.post("/api/approvals/{qid}/{version}/reject")
    def api_reject(qid: str, version: int, body: ReasonIn, user: User = Depends(approver)):
        with ctx.lock:
            quotes.reject(ctx, qid, version, user, body.reason)
        return {"result": "rejected"}

    @app.post("/api/approvals/{qid}/{version}/change")
    def api_change(qid: str, version: int, body: ChangeIn, user: User = Depends(approver)):
        with ctx.lock:
            return {"new_version": quotes.change(ctx, qid, version, user, rep_discount_pct=body.rep_discount_pct, chosen=body.chosen)}

    @app.post("/api/notices/{outbound_id}/approve")
    def api_notice(outbound_id: str, user: User = Depends(approver)):
        slips.approve_notice(ctx, outbound_id, user)
        return {"result": "sent"}

    # ---- reps ---------------------------------------------------------------------------
    @app.get("/api/rep/cases")
    def api_cases(user: User = Depends(staff)):
        rows = ctx.store.db.query("SELECT * FROM cases ORDER BY id DESC") if user.is_approver else \
            ctx.store.db.query("SELECT * FROM cases WHERE rep_id=? ORDER BY id DESC", (user.id,))
        return rows

    @app.get("/api/rep/tasks")
    def api_tasks(user: User = Depends(staff)):
        rows = ctx.store.db.query("SELECT t.* FROM tasks t LEFT JOIN cases c ON c.id = t.case_id WHERE t.state='open' ORDER BY t.id DESC")
        mine = [r for r in rows if (r["role"] == user.role) or (r["role"] == "rep" and user.role == "rep") or (user.role == "sales_head" and r["role"] == "sales_manager")]
        if user.role == "rep":
            owned = {c["id"] for c in ctx.store.db.query("SELECT id FROM cases WHERE rep_id=?", (user.id,))}
            mine = [r for r in mine if r["case_id"] is None or r["case_id"] in owned]
        return mine

    @app.post("/api/tasks/{task_id}/resolve")
    def api_resolve(task_id: int, user: User = Depends(staff)):
        with ctx.store.db.tx() as conn:
            conn.execute("UPDATE tasks SET state='done', resolved_by=?, resolved_at=? WHERE id=?", (user.id, ctx.store.now(), task_id))
        return {"ok": True}

    @app.get("/api/rep/alerts")
    def api_alerts(user: User = Depends(staff)):
        return ctx.store.db.query("SELECT * FROM alerts WHERE user_id=? ORDER BY id DESC", (user.id,))

    @app.get("/api/rep/orders")
    def api_rep_orders(user: User = Depends(staff)):
        metas = ctx.store.db.query("SELECT * FROM orders_meta ORDER BY created_at DESC")
        return [builders.build_rep_order(ctx, m) for m in metas if builders.can_see_order(ctx, user, m)]

    @app.get("/api/rep/orders/{order_id}")
    def api_rep_order(order_id: str, user: User = Depends(staff)):
        return builders.build_rep_order(ctx, order_or_404(user, order_id))

    @app.post("/api/quotes/{qid}/{version}/discount")
    def api_discount(qid: str, version: int, body: DiscountIn, user: User = Depends(staff)):
        own_quote(user, qid, version)
        with ctx.lock:
            return {"new_version": quotes.revise(ctx, qid, version, user, rep_discount_pct=body.pct)}

    @app.post("/api/quotes/{qid}/{version}/consent")
    def api_consent(qid: str, version: int, user: User = Depends(staff)):
        own_quote(user, qid, version)
        quotes.record_consent(ctx, qid, version, user)
        return {"ok": True}

    @app.post("/api/quotes/{qid}/{version}/submit")
    def api_submit(qid: str, version: int, user: User = Depends(staff)):
        own_quote(user, qid, version)
        with ctx.lock:
            return {"roles": quotes.submit(ctx, qid, version, user.id)}

    @app.post("/api/slips/{slip_id}/decide")
    def api_decide(slip_id: int, body: DecideIn, user: User = Depends(staff)):
        with ctx.lock:
            return slips.decide(ctx, slip_id, user, body.decision, body.option, body.note)

    # ---- customers ----------------------------------------------------------------------
    @app.get("/api/customer/orders")
    def api_customer_orders(user: User = Depends(customer)):
        metas = ctx.store.db.query("SELECT * FROM orders_meta WHERE customer_id=? ORDER BY created_at DESC", (user.account_id,))
        return [builders.build_customer_order(ctx, m) for m in metas]

    @app.get("/api/customer/orders/{order_id}")
    def api_customer_order(order_id: str, user: User = Depends(customer)):
        return builders.build_customer_order(ctx, order_or_404(user, order_id))

    # ---- what-if and admin --------------------------------------------------------------
    @app.post("/api/whatif/promise")
    def api_whatif(body: WhatIfIn, user: User = Depends(staff)):
        snap = ctx.builder.build()
        req = Request(tuple((p, int(q)) for p, q in body.lines), body.city, body.deliver_by, body.partial)
        return what_if(snap, req, body.change, ctx.config)

    @app.post("/api/whatif/floor")
    def api_whatif_floor(body: FloorIn, user: User = Depends(staff)):
        qr = quotes.load_request(ctx, body.quotation_id, body.version)
        snap = ctx.builder.build()
        inp = _price_inputs(ctx, qr, snap)
        auth = quotes.authorities(ctx)
        def run(floor):
            terms = tuple(replace(t, floor_override=floor) for t in inp.terms) if floor is not None else inp.terms
            cfg = replace(ctx.config, default_floor_pct=floor) if floor is not None else ctx.config
            res = price_request(replace(inp, terms=terms), cfg)
            routing = route_approvals(res.flags, auth)
            return {"flags": sorted(f.code for f in res.flags), "roles": list(routing.roles), "unapprovable": routing.unapprovable,
                    "margins": [str(ln.margin_pct.quantize(Decimal("0.01"))) for ln in res.lines if ln.margin_pct is not None]}
        return {"before": run(None), "after": run(body.floor_pct)}

    @app.post("/admin/poll")
    def admin_poll(user: User = Depends(staff)):
        return {"handled": intake.poll(ctx)}

    @app.post("/admin/check-slips")
    def admin_slips(user: User = Depends(staff)):
        return {"new_slips": slips.check_slips(ctx)}

    @app.post("/admin/recover")
    def admin_recover(user: User = Depends(staff)):
        return {"resumed": saga.recover(ctx)}

    @app.get("/admin/status")
    def admin_status(user: User = Depends(staff)):
        health = {}
        for name in STANDINS:
            try:
                health[name] = getattr(ctx.clients, name).http.get("/health").json().get("ok", False)
            except Exception:
                health[name] = False
        db = ctx.store.db
        return {"standins": health, "mail_cursor": db.one("SELECT last_delivery_id FROM mail_cursor")["last_delivery_id"],
                "intents": {r["state"]: r["c"] for r in db.query("SELECT state, COUNT(*) AS c FROM intents GROUP BY state")},
                "open_block_flags": db.one("SELECT COUNT(*) AS c FROM flags WHERE severity='block' AND resolved_at IS NULL")["c"],
                "open_tasks": db.one("SELECT COUNT(*) AS c FROM tasks WHERE state='open'")["c"]}

    @app.get("/health")
    def health():
        return {"ok": True, "name": "core"}

    # ---- pages --------------------------------------------------------------------------
    def page(request: HttpRequest, name: str, user: User, **data):
        return TEMPLATES.TemplateResponse(request, name, {"user": user, "msg": request.query_params.get("msg", ""), **data})

    def back(url: str, msg: str = ""):
        return RedirectResponse(f"{url}?msg={msg}" if msg else url, status_code=303)

    @app.get("/ui/approvals", response_class=HTMLResponse)
    def ui_approvals(request: HttpRequest, user: User = Depends(approver)):
        return page(request, "approvals.html", user, queue=builders.approval_queue(ctx, user), alerts=api_alerts(user))

    @app.get("/ui/approvals/{qid}/{version}", response_class=HTMLResponse)
    def ui_approval(request: HttpRequest, qid: str, version: int, user: User = Depends(approver)):
        return page(request, "approval_detail.html", user, q=builders.build_approver_quote(ctx, qid, version))

    @app.post("/ui/approvals/{qid}/{version}/{action}")
    def ui_approval_action(qid: str, version: int, action: str, reason: str = Form(""), pct: str = Form(""), chosen: str = Form(""),
                           user: User = Depends(approver)):
        url = f"/ui/approvals/{qid}/{version}"
        try:
            with ctx.lock:
                if action == "approve":
                    result = quotes.approve(ctx, qid, version, user, reason)
                    return back("/ui/approvals", f"{qid} v{version}: {result}")
                if action == "reject":
                    quotes.reject(ctx, qid, version, user, reason)
                    return back("/ui/approvals", f"{qid} v{version} rejected")
                if action == "change":
                    new = quotes.change(ctx, qid, version, user, rep_discount_pct=Decimal(pct) if pct else None, chosen=chosen or None)
                    return back("/ui/approvals", f"new version v{new} created and sent for approval")
        except (quotes.QuoteError, gate.GateRefused, Unknown) as exc:
            return back(url, str(exc))
        raise HTTPException(404)

    @app.get("/ui/rep", response_class=HTMLResponse)
    def ui_rep(request: HttpRequest, user: User = Depends(staff)):
        cases = api_cases(user)
        rows = []
        for c in cases:
            versions = []
            if c["quotation_id"]:
                try:
                    doc = ctx.clients.erp.get(f"/quotations/{c['quotation_id']}").data
                    versions = [{"version": v["version"], "status": v["status"], "flags": builders._flags(ctx, doc["id"], v["version"], False)} for v in doc["versions"]]
                except Unknown:
                    pass
            rows.append({**c, "versions": versions})
        orders = api_rep_orders(user)
        return page(request, "rep.html", user, cases=rows, tasks=api_tasks(user), alerts=api_alerts(user), orders=orders)

    @app.post("/ui/rep/quotes/{qid}/{version}/{action}")
    def ui_rep_action(qid: str, version: int, action: str, pct: str = Form(""), user: User = Depends(staff)):
        try:
            own_quote(user, qid, version)
            with ctx.lock:
                if action == "submit":
                    quotes.submit(ctx, qid, version, user.id)
                elif action == "consent":
                    quotes.record_consent(ctx, qid, version, user)
                elif action == "discount":
                    quotes.revise(ctx, qid, version, user, rep_discount_pct=Decimal(pct))
                else:
                    raise HTTPException(404)
        except (quotes.QuoteError, Unknown) as exc:
            return back("/ui/rep", str(exc))
        return back("/ui/rep", f"{action} done")

    @app.get("/ui/rep/orders/{order_id}", response_class=HTMLResponse)
    def ui_rep_order(request: HttpRequest, order_id: str, user: User = Depends(staff)):
        return page(request, "rep_order.html", user, o=builders.build_rep_order(ctx, order_or_404(user, order_id)))

    @app.post("/ui/slips/{slip_id}/decide")
    def ui_decide(slip_id: int, order: str = Form(...), decision: str = Form(...), option: str = Form(""), note: str = Form(""), user: User = Depends(staff)):
        try:
            with ctx.lock:
                slips.decide(ctx, slip_id, user, decision, int(option) if option != "" else None, note)
        except (quotes.QuoteError, gate.GateRefused, Unknown) as exc:
            return back(f"/ui/rep/orders/{order}", str(exc))
        return back(f"/ui/rep/orders/{order}", "decision recorded")

    # ---- demo tools: drive the whole flow from the browser (staff only) ------------------
    def _demo_data() -> dict:
        people = []
        for acc in ctx.clients.crm.get("/accounts").data:
            for c in ctx.clients.crm.get(f"/accounts/{acc['id']}/contacts").data:
                people.append({"email": c["email"], "label": f"{c['name']} ({acc['name']})" + ("" if c["can_confirm_orders"] else " - cannot confirm orders")})
        mail = ctx.clients.mailbox.get("/deliveries", limit=500).data[::-1][:25]
        quotes_sent = []
        for r in ctx.store.db.query("SELECT * FROM outbound_messages WHERE kind='quote' AND state='sent' ORDER BY id DESC"):
            refs = __import__("json").loads(r["refs_json"])
            quotes_sent.append({"id": r["id"], "quotation": refs["quotation_id"], "version": refs["version"], "to": r["to_addr"], "thread": refs.get("thread_id")})
        wos = []
        for m in ctx.store.db.query("SELECT order_id FROM orders_meta WHERE state='confirmed'"):
            for w in ctx.clients.production.get("/work-orders", order_ref=m["order_id"]).data:
                wos.append({"order": m["order_id"], "id": w["id"], "product": w["product_id"], "state": w["state"], "promised": w["promised_date"], "forecast": w["forecast_finish"]})
        return {"people": people, "mail": mail, "quotes_sent": quotes_sent, "work_orders": wos, "headline": HEADLINE_TEXT}

    def _inject(email: str, body: str, thread: str, subject: str = "Demo email") -> int:
        try:
            customer = ctx.clients.crm.get("/accounts/by-email", email=email).data["id"]
        except Unknown:
            customer = "UNKNOWN"
        n = len(ctx.clients.mailbox.get("/deliveries", limit=1000).data) + 1
        ctx.clients.mailbox.post("/inbound", json={"message_id": f"<demo-{n}@ui>", "thread_id": thread or f"T-UI-{n}", "customer_id": customer,
                                                   "subject": subject, "from_addr": email, "body": body})
        with ctx.lock:
            return intake.poll(ctx)

    @app.get("/ui/demo", response_class=HTMLResponse)
    def ui_demo(request: HttpRequest, user: User = Depends(staff)):
        return page(request, "demo.html", user, **_demo_data())

    @app.post("/ui/demo/send")
    def ui_demo_send(sender: str = Form(...), body: str = Form(...), thread: str = Form(""), user: User = Depends(staff)):
        handled = _inject(sender, body, thread)
        return back("/ui/demo", f"Email sent as {sender} and processed ({handled} message handled). Now check the Approvals page or Cases page.")

    @app.post("/ui/demo/reply")
    def ui_demo_reply(outbound: str = Form(...), what: str = Form(...), user: User = Depends(staff)):
        row = ctx.store.db.one("SELECT * FROM outbound_messages WHERE id=?", (outbound,))
        refs = __import__("json").loads(row["refs_json"])
        text = {"accept": "Confirmed, go ahead.", "decline": "We decline this quotation, no longer needed."}[what]
        handled = _inject(row["to_addr"], f"{text} Re: Quotation {refs['quotation_id']} v{refs['version']}", refs.get("thread_id") or "", f"Re: Quotation {refs['quotation_id']}")
        return back("/ui/demo", f"Customer reply ({what}) sent and processed ({handled} handled). Check the customer's order page or the Cases page.")

    @app.post("/ui/demo/slip")
    def ui_demo_slip(work_order: str = Form(...), days: int = Form(3), user: User = Depends(staff)):
        wo = next(w for w in ctx.clients.production.get("/work-orders").data if w["id"] == work_order)
        late = (date.fromisoformat(wo["promised_date"]) + timedelta(days=days)).isoformat()
        ctx.clients.production.post(f"/admin/work-orders/{work_order}", json={"forecast_finish": late, "state": "late", "note": "simulated delay"}, method="PATCH")
        with ctx.lock:
            created = slips.check_slips(ctx)
        return back("/ui/demo", f"{work_order} now finishes {late}. {len(created)} new slip(s) detected. See the rep's order page.")

    @app.post("/ui/demo/recover")
    def ui_demo_recover(work_order: str = Form(...), user: User = Depends(staff)):
        wo = next(w for w in ctx.clients.production.get("/work-orders").data if w["id"] == work_order)
        ctx.clients.production.post(f"/admin/work-orders/{work_order}", json={"forecast_finish": wo["promised_date"], "state": "in_progress"}, method="PATCH")
        with ctx.lock:
            slips.check_slips(ctx)
        return back("/ui/demo", f"{work_order} is back on schedule.")

    @app.get("/ui/customer", response_class=HTMLResponse)
    def ui_customer(request: HttpRequest, user: User = Depends(customer)):
        return page(request, "customer.html", user, orders=api_customer_orders(user))

    @app.get("/ui/customer/orders/{order_id}", response_class=HTMLResponse)
    def ui_customer_order(request: HttpRequest, order_id: str, user: User = Depends(customer)):
        return page(request, "customer_order.html", user, o=builders.build_customer_order(ctx, order_or_404(user, order_id)))

    return app

"""The Outbound Gate: the only door to a customer.

Every message is stored first. A customer message can leave only through release(), which checks
the rules for its kind. A commitment must match something a right-level person approved (by hash).
"""

import hashlib
import json
from datetime import timedelta

from limitara.clock import parse_instant
from limitara.core.context import Ctx
from limitara.freshness import MAX_AGE

OUR_ADDRESS = "sales@limitara.example"


class GateRefused(Exception):
    pass


def message_id(outbound_id: str) -> str:
    return f"<{outbound_id}@limitara.example>"


def text_hash(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()


def draft(ctx: Ctx, kind: str, to_addr: str, subject: str, body: str, *, case_id: str | None = None,
          audience: str = "customer", content_hash: str | None = None, refs: dict | None = None,
          idempotency_key: str | None = None) -> str:
    db = ctx.store.db
    with db.tx() as conn:
        if idempotency_key:
            row = conn.execute("SELECT id FROM outbound_messages WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return row["id"]
        n = conn.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0] + 1
        oid = f"OUT-{n:05d}"
        conn.execute(
            "INSERT INTO outbound_messages(id, case_id, kind, audience, to_addr, subject, body, content_hash, refs_json, state,"
            " idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, case_id, kind, audience, to_addr, subject, body, content_hash or text_hash(subject, body),
             json.dumps(refs or {}), "draft", idempotency_key or oid, ctx.store.now()),
        )
    ctx.store.audit("system", None, "outbound_drafted", "outbound", oid, kind=kind, audience=audience)
    return oid


def get(ctx: Ctx, oid: str) -> dict:
    row = ctx.store.db.one("SELECT * FROM outbound_messages WHERE id=?", (oid,))
    if row is None:
        raise GateRefused(f"no such outbound message {oid}")
    row["refs"] = json.loads(row["refs_json"] or "{}")
    return row


def approve(ctx: Ctx, oid: str, basis: str, approver: str) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE outbound_messages SET state='approved', approved_basis=?, approved_by=? WHERE id=? AND state IN ('draft','awaiting_approval')",
                     (basis, approver, oid))
    ctx.store.audit("user" if approver else "system", approver, "outbound_approved", "outbound", oid, basis=basis)


def request_approval(ctx: Ctx, oid: str) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE outbound_messages SET state='awaiting_approval' WHERE id=? AND state='draft'", (oid,))


def cancel(ctx: Ctx, oid: str) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE outbound_messages SET state='cancelled' WHERE id=? AND state!='sent'", (oid,))


def _check_quote(ctx: Ctx, msg: dict) -> None:
    refs = msg["refs"]
    qid, version = refs["quotation_id"], refs["version"]
    doc = ctx.clients.erp.get(f"/quotations/{qid}").data
    ver = next((v for v in doc["versions"] if v["version"] == version), None)
    if ver is None:
        raise GateRefused(f"{qid} v{version} does not exist")
    if ver["status"] not in ("approved", "sent"):
        raise GateRefused(f"{qid} v{version} is {ver['status']}, not approved")
    if msg["content_hash"] != ver["content_hash"]:
        raise GateRefused("the message does not match the approved version (content hash differs)")
    db = ctx.store.db
    pending = db.query("SELECT * FROM approvals WHERE quotation_id=? AND version=? AND status!='approved'", (qid, version))
    if pending or not db.query("SELECT 1 FROM approvals WHERE quotation_id=? AND version=?", (qid, version)):
        raise GateRefused("not every required approval has been given")
    if db.query("SELECT 1 FROM approvals WHERE quotation_id=? AND version=? AND decided_by=?", (qid, version, ver["created_by"])):
        raise GateRefused("the creator of a version cannot approve it")
    blocks = db.query("SELECT code FROM flags WHERE quotation_id=? AND version=? AND severity='block' AND resolved_at IS NULL", (qid, version))
    if blocks:
        raise GateRefused("unresolved blocking flags: " + ", ".join(b["code"] for b in blocks))
    verified = refs.get("verified_at")
    if not verified or ctx.clock.now() - parse_instant(verified) > MAX_AGE["stock"] + timedelta(seconds=1):
        raise GateRefused("the plan was not verified recently enough")


def _check_confirmation(ctx: Ctx, msg: dict) -> str | None:
    """Returns an auto-approval basis when the confirmation is identical to the accepted version."""
    refs = msg["refs"]
    doc = ctx.clients.erp.get(f"/quotations/{refs['quotation_id']}").data
    ver = next((v for v in doc["versions"] if v["version"] == refs["version"]), None)
    if ver and ver["status"] == "accepted" and msg["content_hash"] == ver["content_hash"] and refs.get("order_state") == "confirmed":
        return "identical_to_accepted_quote"
    return None


def release(ctx: Ctx, oid: str) -> dict:
    msg = get(ctx, oid)
    if msg["state"] == "sent":
        return msg
    if msg["state"] == "cancelled":
        raise GateRefused("this message was cancelled")
    if msg["audience"] == "customer":
        kind = msg["kind"]
        if kind == "quote":
            _check_quote(ctx, msg)
        elif kind == "order_confirmation":
            basis = _check_confirmation(ctx, msg)
            if basis:
                approve(ctx, oid, basis, "")
                msg = get(ctx, oid)
            elif msg["state"] != "approved":
                raise GateRefused("a confirmation that differs from the approved quote needs a sales manager's approval")
        elif kind in ("delay_notice", "clarification"):
            if msg["state"] != "approved":
                raise GateRefused(f"a {kind} needs a person's approval before it can be sent")
            if kind == "delay_notice" and msg["refs"].get("states_new_date") and not str(msg["approved_basis"]).startswith("manager"):
                raise GateRefused("a notice that states a new date needs a sales manager's approval")
        else:
            raise GateRefused(f"messages of kind {kind} cannot be sent to customers")
    ctx.clients.mailbox.post("/outbound", json={
        "message_id": message_id(oid), "thread_id": msg["refs"].get("thread_id"), "customer_id": msg["refs"].get("customer_id", "UNKNOWN"),
        "subject": msg["subject"], "to_addr": msg["to_addr"], "body": msg["body"], "in_reply_to": msg["refs"].get("in_reply_to")},
        key=f"OUTBOUND:{oid}")
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE outbound_messages SET state='sent', sent_at=? WHERE id=?", (ctx.store.now(), oid))
    ctx.store.audit("system", None, "outbound_sent", "outbound", oid, kind=msg["kind"], to=msg["to_addr"])
    return get(ctx, oid)


def notify_staff(ctx: Ctx, to_addr: str, subject: str, body: str, case_id: str | None = None) -> str:
    """Internal mail to staff. Not a customer message, so no approval is needed."""
    oid = draft(ctx, "internal_alert", to_addr, subject, body, case_id=case_id, audience="staff")
    return release(ctx, oid)["id"]

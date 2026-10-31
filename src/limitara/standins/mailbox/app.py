"""Mailbox stand-in: threads, deliveries (in and out), attachments."""

from fastapi import FastAPI, Header
from pydantic import BaseModel

from limitara.clock import Clock, clock_from_env, to_wire
from limitara.db import Database, db_path
from limitara.standins.base import ApiError, create_base_app, idempotent, stamped


class AttachmentIn(BaseModel):
    filename: str
    content_type: str = "text/plain"
    content: str


class InboundIn(BaseModel):
    message_id: str
    thread_id: str | None = None
    customer_id: str = "UNKNOWN"
    subject: str = ""
    from_addr: str
    to_addr: str = "sales@limitara.example"
    body: str
    in_reply_to: str | None = None
    sent_at: str | None = None
    received_at: str | None = None
    attachments: list[AttachmentIn] = []


class OutboundIn(BaseModel):
    message_id: str
    thread_id: str | None = None
    customer_id: str = "UNKNOWN"
    subject: str = ""
    from_addr: str = "sales@limitara.example"
    to_addr: str
    body: str
    in_reply_to: str | None = None


def _delivery_view(conn, delivery_id: int) -> dict:
    row = dict(conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone())
    row["attachments"] = [
        dict(a) for a in conn.execute("SELECT filename, content_type, content FROM attachments WHERE delivery_id=?", (delivery_id,))
    ]
    return row


def _insert(conn, body, direction: str, now: str, sent_at: str, received_at: str) -> int:
    thread_id = body.thread_id
    if thread_id is None or conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
        thread_id = thread_id or f"T-NEW-{conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0] + 1:04d}"
        conn.execute("INSERT INTO threads VALUES (?,?,?)", (thread_id, body.customer_id, body.subject))
    cur = conn.execute(
        "INSERT INTO deliveries(message_id, thread_id, in_reply_to, from_addr, to_addr, sent_at, received_at, body,"
        " direction, subject) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (body.message_id, thread_id, body.in_reply_to, body.from_addr, body.to_addr, sent_at, received_at,
         body.body, direction, body.subject),
    )
    for a in getattr(body, "attachments", []):
        conn.execute(
            "INSERT INTO attachments(delivery_id, filename, content_type, content) VALUES (?,?,?,?)",
            (cur.lastrowid, a.filename, a.content_type, a.content),
        )
    return cur.lastrowid


def create_app(path: str | None = None, clock: Clock | None = None) -> FastAPI:
    db = Database(path or db_path("mailbox"))
    app = create_base_app("mailbox", db, clock or clock_from_env())

    def now_iso() -> str:
        return to_wire(app.state.clock.now())

    @app.get("/deliveries")
    def deliveries(after_id: int = 0, limit: int = 50):
        ids = db.query("SELECT delivery_id FROM deliveries WHERE delivery_id>? ORDER BY delivery_id LIMIT ?", (after_id, limit))
        with db.lock:
            return stamped(app, [_delivery_view(db.conn, r["delivery_id"]) for r in ids])

    @app.get("/threads/{thread_id}")
    def thread(thread_id: str):
        head = db.one("SELECT * FROM threads WHERE id=?", (thread_id,))
        if head is None:
            raise ApiError(404, "no_such_thread")
        ids = db.query("SELECT delivery_id FROM deliveries WHERE thread_id=? ORDER BY sent_at, delivery_id", (thread_id,))
        with db.lock:
            head["deliveries"] = [_delivery_view(db.conn, r["delivery_id"]) for r in ids]
        return stamped(app, head)

    @app.get("/threads")
    def threads(customer_id: str | None = None):
        sql, params = "SELECT * FROM threads", ()
        if customer_id:
            sql, params = sql + " WHERE customer_id=?", (customer_id,)
        return stamped(app, db.query(sql + " ORDER BY id", params))

    @app.post("/inbound")
    def inbound(body: InboundIn):
        now = now_iso()
        with db.tx() as conn:
            did = _insert(conn, body, "in", now, body.sent_at or now, body.received_at or now)
            return _delivery_view(conn, did)

    @app.post("/outbound")
    def outbound(body: OutboundIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            return _delivery_view(conn, _insert(conn, body, "out", now, now, now))

        return idempotent(app, idempotency_key, body.model_dump(), work)

    return app

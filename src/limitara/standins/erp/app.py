"""ERP stand-in: items, stock, price lists, reservations, quotation documents, sales orders."""

from fastapi import FastAPI, Header
from pydantic import BaseModel

from limitara.clock import Clock, clock_from_env, parse_instant
from limitara.db import Database, db_path
from limitara.quote_states import IllegalTransition, next_state
from limitara.standins.base import ApiError, create_base_app, idempotent, stamped


class ResItem(BaseModel):
    location_id: str
    item_id: str
    qty: int


class ReservationIn(BaseModel):
    ref_id: str
    kind: str = "soft"
    expires_at: str | None = None
    items: list[ResItem]


class LineIn(BaseModel):
    product_id: str
    qty: int
    list_price: str
    discount_pct: str = "0.00"
    unit_price: str
    price_list_id: str | None = None
    promised_delivery_date: str | None = None


class VersionIn(BaseModel):
    requested_delivery_date: str | None = None
    valid_until: str
    interpretation_note: str = ""
    total_ex_tax: str
    content_hash: str
    created_by: str
    supersedes: int | None = None
    lines: list[LineIn]


class QuotationIn(VersionIn):
    customer_id: str
    case_ref: str | None = None


class TransitionIn(BaseModel):
    to: str
    actor: str = "system"
    reason: str = ""


class OrderLineIn(BaseModel):
    product_id: str
    qty: int
    unit_price: str


class OrderIn(BaseModel):
    quotation_id: str
    version: int
    customer_id: str
    lines: list[OrderLineIn]


class OrderTransitionIn(BaseModel):
    to: str


class StockAdjustIn(BaseModel):
    location_id: str
    item_id: str
    on_hand_delta: int


def _release(conn, reservation_id: str, new_status: str) -> None:
    for it in conn.execute("SELECT * FROM reservation_items WHERE reservation_id=?", (reservation_id,)).fetchall():
        conn.execute(
            "UPDATE stock SET reserved = reserved - ? WHERE location_id=? AND item_id=?",
            (it["qty"], it["location_id"], it["item_id"]),
        )
    conn.execute("UPDATE reservations SET status=? WHERE id=?", (new_status, reservation_id))


def _expire_due(conn, now_iso: str) -> None:
    now = parse_instant(now_iso)
    rows = conn.execute(
        "SELECT id, expires_at FROM reservations WHERE status='active' AND kind='soft' AND expires_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        if parse_instant(row["expires_at"]) < now:
            _release(conn, row["id"], "expired")


def _expire_quotes(conn, now_iso: str) -> None:
    now = parse_instant(now_iso)
    for row in conn.execute("SELECT quotation_id, version, valid_until FROM quotation_versions WHERE status='sent'").fetchall():
        if parse_instant(row["valid_until"]) < now:
            conn.execute(
                "UPDATE quotation_versions SET status='expired' WHERE quotation_id=? AND version=?",
                (row["quotation_id"], row["version"]),
            )


def _reservation_view(conn, rid: str) -> dict:
    head = dict(conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
    head["items"] = [dict(r) for r in conn.execute("SELECT location_id, item_id, qty FROM reservation_items WHERE reservation_id=?", (rid,))]
    return head


def _quote_view(conn, qid: str) -> dict | None:
    head = conn.execute("SELECT * FROM quotations WHERE id=?", (qid,)).fetchone()
    if head is None:
        return None
    out = dict(head)
    out["versions"] = []
    for v in conn.execute("SELECT * FROM quotation_versions WHERE quotation_id=? ORDER BY version", (qid,)).fetchall():
        ver = dict(v)
        ver["lines"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM quotation_lines WHERE quotation_id=? AND version=? ORDER BY line_no", (qid, v["version"])
            )
        ]
        out["versions"].append(ver)
    return out


def _insert_version(conn, qid: str, version: int, body: VersionIn, status: str, now: str) -> None:
    conn.execute(
        "INSERT INTO quotation_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (qid, version, status, body.requested_delivery_date, body.valid_until, body.interpretation_note,
         body.total_ex_tax, body.content_hash, body.supersedes, body.created_by, now),
    )
    _insert_lines(conn, qid, version, body.lines)


def _insert_lines(conn, qid: str, version: int, lines: list[LineIn]) -> None:
    for n, ln in enumerate(lines, start=1):
        conn.execute(
            "INSERT INTO quotation_lines VALUES (?,?,?,?,?,?,?,?,?,?)",
            (qid, version, n, ln.product_id, ln.qty, ln.list_price, ln.discount_pct, ln.unit_price,
             ln.price_list_id, ln.promised_delivery_date),
        )


def create_app(path: str | None = None, clock: Clock | None = None) -> FastAPI:
    db = Database(path or db_path("erp"))
    app = create_base_app("erp", db, clock or clock_from_env())

    def now_iso() -> str:
        from limitara.clock import to_wire

        return to_wire(app.state.clock.now())

    @app.get("/items")
    def items():
        return stamped(app, db.query("SELECT * FROM items ORDER BY id"))

    @app.get("/bom")
    def bom(product_id: str | None = None):
        sql, params = "SELECT * FROM bom", ()
        if product_id:
            sql, params = sql + " WHERE product_id=?", (product_id,)
        return stamped(app, db.query(sql, params))

    @app.get("/stock")
    def stock(item_id: str | None = None, location_id: str | None = None):
        with db.tx() as conn:
            _expire_due(conn, now_iso())
        sql, params, where = "SELECT * FROM stock", [], []
        if item_id:
            where.append("item_id=?"); params.append(item_id)
        if location_id:
            where.append("location_id=?"); params.append(location_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return stamped(app, db.query(sql + " ORDER BY location_id, item_id", tuple(params)))

    @app.get("/locations")
    def locations():
        return stamped(app, db.query("SELECT * FROM locations ORDER BY id"))

    @app.get("/price-lists")
    def price_lists(customer_id: str | None = None):
        rows = db.query(
            "SELECT * FROM price_lists WHERE customer_id IS NULL OR customer_id=? ORDER BY id", (customer_id,)
        )
        for r in rows:
            r["items"] = db.query("SELECT product_id, unit_price FROM price_list_items WHERE price_list_id=?", (r["id"],))
        return stamped(app, rows)

    # ---- reservations -------------------------------------------------------------------
    @app.post("/reservations")
    def reserve(body: ReservationIn, idempotency_key: str | None = Header(None)):
        if body.kind not in ("soft", "hard"):
            raise ApiError(422, "bad_kind")
        if body.kind == "soft" and not body.expires_at:
            raise ApiError(422, "soft_needs_expiry")

        def work(conn, now):
            _expire_due(conn, now)
            for it in body.items:
                row = conn.execute(
                    "SELECT on_hand - reserved AS free FROM stock WHERE location_id=? AND item_id=?",
                    (it.location_id, it.item_id),
                ).fetchone()
                if row is None or row["free"] < it.qty:
                    raise ApiError(422, "insufficient_stock", f"{it.item_id} at {it.location_id}: need {it.qty}")
            n = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] + 1
            rid = f"RES-{n:04d}"
            conn.execute(
                "INSERT INTO reservations VALUES (?,?,?,?,?,?)",
                (rid, body.ref_id, body.kind, "active", body.expires_at if body.kind == "soft" else None, now),
            )
            for it in body.items:
                conn.execute("INSERT INTO reservation_items VALUES (?,?,?,?)", (rid, it.location_id, it.item_id, it.qty))
                conn.execute(
                    "UPDATE stock SET reserved = reserved + ? WHERE location_id=? AND item_id=?",
                    (it.qty, it.location_id, it.item_id),
                )
            return _reservation_view(conn, rid)

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.post("/reservations/{rid}/convert")
    def convert(rid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            _expire_due(conn, now)
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_reservation")
            if row["status"] != "active":
                raise ApiError(422, "not_active", f"reservation is {row['status']}")
            conn.execute("UPDATE reservations SET kind='hard', expires_at=NULL WHERE id=?", (rid,))
            return _reservation_view(conn, rid)

        return idempotent(app, idempotency_key, {"rid": rid, "op": "convert"}, work)

    @app.post("/reservations/{rid}/release")
    def release(rid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_reservation")
            if row["status"] == "active":
                _release(conn, rid, "released")
            return _reservation_view(conn, rid)

        return idempotent(app, idempotency_key, {"rid": rid, "op": "release"}, work)

    @app.get("/reservations")
    def reservations(ref_id: str | None = None):
        with db.tx() as conn:
            _expire_due(conn, now_iso())
            sql, params = "SELECT id FROM reservations", ()
            if ref_id:
                sql, params = sql + " WHERE ref_id=?", (ref_id,)
            rows = [_reservation_view(conn, r["id"]) for r in conn.execute(sql, params).fetchall()]
        return stamped(app, rows)

    # ---- quotation documents ------------------------------------------------------------
    @app.post("/quotations")
    def create_quotation(body: QuotationIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            n = conn.execute("SELECT COUNT(*) FROM quotations WHERE id LIKE 'Q-2%'").fetchone()[0] + 1
            qid = f"Q-{now[:4]}-{n:04d}"
            conn.execute("INSERT INTO quotations VALUES (?,?,?,?)", (qid, body.customer_id, body.case_ref, now))
            _insert_version(conn, qid, 1, body, "draft", now)
            return _quote_view(conn, qid)

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.post("/quotations/{qid}/versions")
    def add_version(qid: str, body: VersionIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            if conn.execute("SELECT 1 FROM quotations WHERE id=?", (qid,)).fetchone() is None:
                raise ApiError(404, "no_such_quotation")
            n = conn.execute("SELECT MAX(version) FROM quotation_versions WHERE quotation_id=?", (qid,)).fetchone()[0] + 1
            _insert_version(conn, qid, n, body, "draft", now)
            return _quote_view(conn, qid)

        return idempotent(app, idempotency_key, {"qid": qid, **body.model_dump()}, work)

    @app.put("/quotations/{qid}/versions/{version}")
    def replace_draft(qid: str, version: int, body: VersionIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            row = conn.execute(
                "SELECT status FROM quotation_versions WHERE quotation_id=? AND version=?", (qid, version)
            ).fetchone()
            if row is None:
                raise ApiError(404, "no_such_version")
            if row["status"] != "draft":
                raise ApiError(422, "frozen", f"version is {row['status']}; create a new version instead")
            conn.execute(
                "UPDATE quotation_versions SET requested_delivery_date=?, valid_until=?, interpretation_note=?,"
                " total_ex_tax=?, content_hash=?, supersedes=? WHERE quotation_id=? AND version=?",
                (body.requested_delivery_date, body.valid_until, body.interpretation_note, body.total_ex_tax,
                 body.content_hash, body.supersedes, qid, version),
            )
            conn.execute("DELETE FROM quotation_lines WHERE quotation_id=? AND version=?", (qid, version))
            _insert_lines(conn, qid, version, body.lines)
            return _quote_view(conn, qid)

        return idempotent(app, idempotency_key, {"qid": qid, "v": version, **body.model_dump()}, work)

    @app.get("/quotations/{qid}")
    def get_quotation(qid: str):
        with db.tx() as conn:
            _expire_quotes(conn, now_iso())
            view = _quote_view(conn, qid)
        if view is None:
            raise ApiError(404, "no_such_quotation")
        return stamped(app, view)

    @app.get("/quotations")
    def list_quotations(customer_id: str | None = None):
        with db.tx() as conn:
            _expire_quotes(conn, now_iso())
            sql, params = "SELECT id FROM quotations ORDER BY id", ()
            if customer_id:
                sql, params = "SELECT id FROM quotations WHERE customer_id=? ORDER BY id", (customer_id,)
            rows = [_quote_view(conn, r["id"]) for r in conn.execute(sql, params).fetchall()]
        return stamped(app, rows)

    @app.post("/quotations/{qid}/versions/{version}/transition")
    def transition(qid: str, version: int, body: TransitionIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            _expire_quotes(conn, now)
            row = conn.execute(
                "SELECT status FROM quotation_versions WHERE quotation_id=? AND version=?", (qid, version)
            ).fetchone()
            if row is None:
                raise ApiError(404, "no_such_version")
            try:
                new = next_state(row["status"], body.to)
            except IllegalTransition as exc:
                raise ApiError(422, "illegal_transition", str(exc)) from exc
            if new == "sent":  # sending a newer version supersedes the live one, in the same step
                conn.execute(
                    "UPDATE quotation_versions SET status='superseded' WHERE quotation_id=? AND status='sent'", (qid,)
                )
            conn.execute(
                "UPDATE quotation_versions SET status=? WHERE quotation_id=? AND version=?", (new, qid, version)
            )
            return _quote_view(conn, qid)

        return idempotent(app, idempotency_key, {"qid": qid, "v": version, **body.model_dump()}, work)

    # ---- sales orders -------------------------------------------------------------------
    def _order_view(conn, oid: str) -> dict:
        head = dict(conn.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone())
        head["lines"] = [dict(r) for r in conn.execute("SELECT * FROM sales_order_lines WHERE order_id=? ORDER BY line_no", (oid,))]
        return head

    @app.post("/sales-orders")
    def create_order(body: OrderIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            ver = conn.execute(
                "SELECT status FROM quotation_versions WHERE quotation_id=? AND version=?",
                (body.quotation_id, body.version),
            ).fetchone()
            if ver is None:
                raise ApiError(404, "no_such_version")
            if ver["status"] != "accepted":
                raise ApiError(422, "not_accepted", f"version is {ver['status']}")
            existing = conn.execute(
                "SELECT id FROM sales_orders WHERE quotation_id=? AND version=?", (body.quotation_id, body.version)
            ).fetchone()
            if existing:
                raise ApiError(422, "order_exists", existing["id"])
            n = conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0]
            oid = f"SO-{1001 + n}"
            conn.execute(
                "INSERT INTO sales_orders VALUES (?,?,?,?,?,?)",
                (oid, body.quotation_id, body.version, body.customer_id, "created", now),
            )
            for i, ln in enumerate(body.lines, start=1):
                conn.execute("INSERT INTO sales_order_lines VALUES (?,?,?,?,?)", (oid, i, ln.product_id, ln.qty, ln.unit_price))
            return _order_view(conn, oid)

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.get("/sales-orders/{oid}")
    def get_order(oid: str):
        with db.lock:
            if db.conn.execute("SELECT 1 FROM sales_orders WHERE id=?", (oid,)).fetchone() is None:
                raise ApiError(404, "no_such_order")
            return stamped(app, _order_view(db.conn, oid))

    @app.get("/sales-orders")
    def list_orders(customer_id: str | None = None):
        ids = db.query(
            "SELECT id FROM sales_orders WHERE (? IS NULL OR customer_id=?) ORDER BY id", (customer_id, customer_id)
        )
        with db.lock:
            return stamped(app, [_order_view(db.conn, r["id"]) for r in ids])

    @app.post("/sales-orders/{oid}/transition")
    def order_transition(oid: str, body: OrderTransitionIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            if conn.execute("SELECT 1 FROM sales_orders WHERE id=?", (oid,)).fetchone() is None:
                raise ApiError(404, "no_such_order")
            if body.to not in ("confirmed", "cancelled"):
                raise ApiError(422, "bad_status")
            conn.execute("UPDATE sales_orders SET status=? WHERE id=?", (body.to, oid))
            return _order_view(conn, oid)

        return idempotent(app, idempotency_key, {"oid": oid, **body.model_dump()}, work)

    @app.post("/admin/stock-adjust")
    def stock_adjust(body: StockAdjustIn):
        with db.tx() as conn:
            conn.execute(
                "UPDATE stock SET on_hand = on_hand + ? WHERE location_id=? AND item_id=?",
                (body.on_hand_delta, body.location_id, body.item_id),
            )
        return {"ok": True}

    return app

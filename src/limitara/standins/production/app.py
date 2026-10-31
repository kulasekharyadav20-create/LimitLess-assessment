"""Production stand-in: plants, lines, daily capacity, bookings, work orders."""

from datetime import date, timedelta

from fastapi import FastAPI, Header
from pydantic import BaseModel

from limitara.clock import Clock, clock_from_env, parse_instant, to_wire
from limitara.db import Database, db_path
from limitara.standins.base import ApiError, create_base_app, idempotent, stamped


class DayIn(BaseModel):
    line_id: str
    day: str
    product_id: str
    units: int


class BookingIn(BaseModel):
    ref_id: str
    kind: str = "soft"
    expires_at: str | None = None
    days: list[DayIn]


class WorkOrderIn(BaseModel):
    order_ref: str
    customer_id: str | None = None
    product_id: str
    plant_id: str
    line_id: str
    qty: int
    promised_date: str
    forecast_finish: str
    booking_id: str | None = None


class LineDownIn(BaseModel):
    from_day: str
    to_day: str
    note: str = "Line down"


class WorkOrderPatch(BaseModel):
    forecast_finish: str | None = None
    state: str | None = None
    completed_qty: int | None = None
    note: str | None = None


def _release(conn, booking_id: str, new_status: str) -> None:
    for d in conn.execute("SELECT * FROM booking_days WHERE booking_id=?", (booking_id,)).fetchall():
        conn.execute(
            "UPDATE capacity SET booked_units = MAX(0, booked_units - ?) WHERE line_id=? AND day=?",
            (d["units"], d["line_id"], d["day"]),
        )
    conn.execute("UPDATE bookings SET status=? WHERE id=?", (new_status, booking_id))


def _expire_due(conn, now_iso: str) -> None:
    now = parse_instant(now_iso)
    for row in conn.execute(
        "SELECT id, expires_at FROM bookings WHERE status='active' AND kind='soft' AND expires_at IS NOT NULL"
    ).fetchall():
        if parse_instant(row["expires_at"]) < now:
            _release(conn, row["id"], "expired")


def _booking_view(conn, bid: str) -> dict:
    head = dict(conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone())
    head["days"] = [dict(r) for r in conn.execute("SELECT line_id, day, product_id, units FROM booking_days WHERE booking_id=?", (bid,))]
    return head


def create_app(path: str | None = None, clock: Clock | None = None) -> FastAPI:
    db = Database(path or db_path("production"))
    app = create_base_app("production", db, clock or clock_from_env())

    def now_iso() -> str:
        return to_wire(app.state.clock.now())

    @app.get("/plants")
    def plants():
        return stamped(app, db.query("SELECT * FROM plants ORDER BY id"))

    @app.get("/lines")
    def lines():
        rows = db.query("SELECT * FROM lines ORDER BY id")
        for r in rows:
            r["products"] = [p["product_id"] for p in db.query("SELECT product_id FROM line_products WHERE line_id=?", (r["id"],))]
        return stamped(app, rows)

    @app.get("/capacity")
    def capacity(plant_id: str | None = None, from_day: str | None = None, to_day: str | None = None):
        with db.tx() as conn:
            _expire_due(conn, now_iso())
        sql = ("SELECT c.line_id, l.plant_id, c.day, c.total_units, c.booked_units, c.status, c.note "
               "FROM capacity c JOIN lines l ON l.id = c.line_id WHERE 1=1")
        params: list = []
        if plant_id:
            sql += " AND l.plant_id=?"; params.append(plant_id)
        if from_day:
            sql += " AND c.day>=?"; params.append(from_day)
        if to_day:
            sql += " AND c.day<=?"; params.append(to_day)
        rows = db.query(sql + " ORDER BY c.line_id, c.day", tuple(params))
        if not rows:
            raise ApiError(404, "no_capacity_records", "no rows for that range: unknown, not zero")
        return stamped(app, rows)

    @app.post("/bookings")
    def book(body: BookingIn, idempotency_key: str | None = Header(None)):
        if body.kind not in ("soft", "hard"):
            raise ApiError(422, "bad_kind")
        if body.kind == "soft" and not body.expires_at:
            raise ApiError(422, "soft_needs_expiry")

        def work(conn, now):
            _expire_due(conn, now)
            for d in body.days:
                if conn.execute("SELECT 1 FROM line_products WHERE line_id=? AND product_id=?", (d.line_id, d.product_id)).fetchone() is None:
                    raise ApiError(422, "line_cannot_make_product", f"{d.line_id} cannot make {d.product_id}")
            need: dict[tuple, int] = {}
            for d in body.days:
                need[(d.line_id, d.day)] = need.get((d.line_id, d.day), 0) + d.units
            for (line, day), units in need.items():
                row = conn.execute("SELECT * FROM capacity WHERE line_id=? AND day=?", (line, day)).fetchone()
                if row is None or row["status"] != "open" or row["total_units"] - row["booked_units"] < units:
                    raise ApiError(422, "insufficient_capacity", f"{line} on {day}: need {units}")
            n = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] + 1
            bid = f"BK-{n:04d}"
            conn.execute(
                "INSERT INTO bookings VALUES (?,?,?,?,?,?)",
                (bid, body.ref_id, body.kind, "active", body.expires_at if body.kind == "soft" else None, now),
            )
            for d in body.days:
                conn.execute("INSERT INTO booking_days VALUES (?,?,?,?,?)", (bid, d.line_id, d.day, d.product_id, d.units))
                conn.execute("UPDATE capacity SET booked_units = booked_units + ? WHERE line_id=? AND day=?", (d.units, d.line_id, d.day))
            return _booking_view(conn, bid)

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.post("/bookings/{bid}/convert")
    def convert(bid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            _expire_due(conn, now)
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_booking")
            if row["status"] != "active":
                raise ApiError(422, "not_active", f"booking is {row['status']}")
            conn.execute("UPDATE bookings SET kind='hard', expires_at=NULL WHERE id=?", (bid,))
            return _booking_view(conn, bid)

        return idempotent(app, idempotency_key, {"bid": bid, "op": "convert"}, work)

    @app.post("/bookings/{bid}/release")
    def release(bid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_booking")
            if row["status"] == "active":
                _release(conn, bid, "released")
            return _booking_view(conn, bid)

        return idempotent(app, idempotency_key, {"bid": bid, "op": "release"}, work)

    @app.get("/bookings")
    def bookings(ref_id: str | None = None):
        with db.tx() as conn:
            _expire_due(conn, now_iso())
            sql, params = "SELECT id FROM bookings", ()
            if ref_id:
                sql, params = sql + " WHERE ref_id=?", (ref_id,)
            rows = [_booking_view(conn, r["id"]) for r in conn.execute(sql, params).fetchall()]
        return stamped(app, rows)

    @app.post("/work-orders")
    def create_work_order(body: WorkOrderIn, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            n = conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
            wid = f"WO-{1004 + n}"
            conn.execute(
                "INSERT INTO work_orders(id, product_id, plant_id, line_id, qty, completed_qty, state, promised_date,"
                " forecast_finish, customer_id, sales_order_ref, note, updated_at, booking_id)"
                " VALUES (?,?,?,?,?,0,'planned',?,?,?,?,NULL,?,?)",
                (wid, body.product_id, body.plant_id, body.line_id, body.qty, body.promised_date,
                 body.forecast_finish, body.customer_id, body.order_ref, now, body.booking_id),
            )
            return dict(conn.execute("SELECT * FROM work_orders WHERE id=?", (wid,)).fetchone())

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.post("/work-orders/{wid}/cancel")
    def cancel_work_order(wid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            row = conn.execute("SELECT * FROM work_orders WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_work_order")
            if row["state"] == "cancelled":
                return dict(row)
            if row["state"] != "planned":
                raise ApiError(422, "already_started", f"work order is {row['state']}")
            conn.execute("UPDATE work_orders SET state='cancelled', updated_at=? WHERE id=?", (now, wid))
            return dict(conn.execute("SELECT * FROM work_orders WHERE id=?", (wid,)).fetchone())

        return idempotent(app, idempotency_key, {"wid": wid, "op": "cancel"}, work)

    @app.get("/work-orders")
    def work_orders(order_ref: str | None = None, state: str | None = None):
        sql, params = "SELECT * FROM work_orders WHERE 1=1", []
        if order_ref:
            sql += " AND sales_order_ref=?"; params.append(order_ref)
        if state:
            sql += " AND state=?"; params.append(state)
        return stamped(app, db.query(sql + " ORDER BY id", tuple(params)))

    @app.post("/admin/lines/{line_id}/down")
    def line_down(line_id: str, body: LineDownIn):
        with db.tx() as conn:
            conn.execute(
                "UPDATE capacity SET total_units=0, booked_units=0, status='down', note=? "
                "WHERE line_id=? AND day>=? AND day<=?",
                (body.note, line_id, body.from_day, body.to_day),
            )
            late = date.fromisoformat(body.to_day) + timedelta(days=2)
            conn.execute(
                "UPDATE work_orders SET state='late', forecast_finish=?, note=?, updated_at=? "
                "WHERE line_id=? AND state IN ('planned','in_progress','late') AND promised_date>=? AND promised_date<=?",
                (late.isoformat(), body.note, now_iso(), line_id, body.from_day, body.to_day),
            )
        return {"ok": True}

    @app.patch("/admin/work-orders/{wid}")
    def patch_work_order(wid: str, body: WorkOrderPatch):
        with db.tx() as conn:
            row = conn.execute("SELECT * FROM work_orders WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_work_order")
            data = {k: v for k, v in body.model_dump().items() if v is not None}
            for k, v in data.items():
                conn.execute(f"UPDATE work_orders SET {k}=?, updated_at=? WHERE id=?", (v, now_iso(), wid))
            return dict(conn.execute("SELECT * FROM work_orders WHERE id=?", (wid,)).fetchone())

    return app

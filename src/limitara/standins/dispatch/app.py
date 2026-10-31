"""Dispatch stand-in: warehouses, lanes, carrier schedules, holidays, shipments and freight."""

from datetime import date
from decimal import Decimal

from fastapi import FastAPI, Header
from pydantic import BaseModel

from limitara.calendar import delivery_date, is_carrier_day, parse_weekdays
from limitara.clock import Clock, clock_from_env, ist_date
from limitara.db import Database, db_path
from limitara.money import to_wire as money_wire
from limitara.standins.base import ApiError, create_base_app, idempotent, stamped


class LineIn(BaseModel):
    product_id: str
    units: int


class ShipmentIn(BaseModel):
    order_ref: str
    customer_id: str
    kind: str = "customer"
    from_location: str
    destination: str
    ship_date: str
    lines: list[LineIn]


class StatusIn(BaseModel):
    status: str


def _holidays(conn) -> frozenset[date]:
    return frozenset(date.fromisoformat(r["day"]) for r in conn.execute("SELECT day FROM holidays"))


def _view(conn, sid: str) -> dict:
    head = dict(conn.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone())
    head["lines"] = [dict(r) for r in conn.execute("SELECT product_id, units FROM shipment_lines WHERE shipment_id=?", (sid,))]
    return head


def create_app(path: str | None = None, clock: Clock | None = None) -> FastAPI:
    db = Database(path or db_path("dispatch"))
    app = create_base_app("dispatch", db, clock or clock_from_env())

    @app.get("/warehouses")
    def warehouses():
        return stamped(app, db.query("SELECT * FROM warehouses ORDER BY id"))

    @app.get("/lanes")
    def lanes():
        return stamped(app, db.query("SELECT * FROM transfer_lanes ORDER BY from_plant, to_warehouse"))

    @app.get("/schedules")
    def schedules(warehouse_id: str | None = None, city: str | None = None):
        sql, params = "SELECT * FROM schedules WHERE 1=1", []
        if warehouse_id:
            sql += " AND warehouse_id=?"; params.append(warehouse_id)
        if city:
            sql += " AND destination_city=?"; params.append(city)
        return stamped(app, db.query(sql + " ORDER BY warehouse_id, destination_city", tuple(params)))

    @app.get("/holidays")
    def holidays(from_day: str | None = None, to_day: str | None = None):
        sql, params = "SELECT * FROM holidays WHERE 1=1", []
        if from_day:
            sql += " AND day>=?"; params.append(from_day)
        if to_day:
            sql += " AND day<=?"; params.append(to_day)
        return stamped(app, db.query(sql + " ORDER BY day", tuple(params)))

    @app.post("/shipments")
    def create_shipment(body: ShipmentIn, idempotency_key: str | None = Header(None)):
        if body.kind not in ("customer", "transfer"):
            raise ApiError(422, "bad_kind")
        units = sum(ln.units for ln in body.lines)
        if units <= 0:
            raise ApiError(422, "no_units")

        def work(conn, now):
            hol = _holidays(conn)
            ship = date.fromisoformat(body.ship_date)
            if body.kind == "customer":
                row = conn.execute(
                    "SELECT * FROM schedules WHERE warehouse_id=? AND destination_city=?",
                    (body.from_location, body.destination),
                ).fetchone()
                if row is None:
                    raise ApiError(422, "no_service", f"{body.from_location} does not serve {body.destination}")
                if not is_carrier_day(ship, parse_weekdays(row["weekdays"]), hol):
                    raise ApiError(422, "not_a_carrier_day", body.ship_date)
                if ship < ist_date(app.state.clock.now()):
                    raise ApiError(422, "ship_date_in_past", body.ship_date)
                charge = Decimal(row["base_charge"]) + Decimal(row["per_unit_charge"]) * units
                deliver, carrier = delivery_date(ship, row["transit_days"], hol), row["carrier"]
            else:
                lane = conn.execute(
                    "SELECT * FROM transfer_lanes WHERE from_plant=? AND to_warehouse=?",
                    (body.from_location, body.destination),
                ).fetchone()
                if lane is None:
                    raise ApiError(422, "no_lane", f"{body.from_location} to {body.destination}")
                charge = Decimal(lane["per_unit_charge"]) * units
                deliver, carrier = delivery_date(ship, lane["transit_days"], hol), "internal"
            n = conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
            sid = f"SHP-{2312 + n}"
            conn.execute(
                "INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, body.order_ref, body.customer_id, body.kind, body.from_location, body.destination, carrier,
                 body.ship_date, deliver.isoformat(), "planned", money_wire(charge)),
            )
            for ln in body.lines:
                conn.execute("INSERT INTO shipment_lines VALUES (?,?,?)", (sid, ln.product_id, ln.units))
            return _view(conn, sid)

        return idempotent(app, idempotency_key, body.model_dump(), work)

    @app.post("/shipments/{sid}/cancel")
    def cancel(sid: str, idempotency_key: str | None = Header(None)):
        def work(conn, now):
            row = conn.execute("SELECT status FROM shipments WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise ApiError(404, "no_such_shipment")
            if row["status"] in ("dispatched", "delivered"):
                raise ApiError(422, "already_dispatched", row["status"])
            conn.execute("UPDATE shipments SET status='cancelled' WHERE id=?", (sid,))
            return _view(conn, sid)

        return idempotent(app, idempotency_key, {"sid": sid, "op": "cancel"}, work)

    @app.get("/shipments")
    def shipments(order_ref: str | None = None):
        sql, params = "SELECT id FROM shipments", ()
        if order_ref:
            sql, params = sql + " WHERE order_ref=?", (order_ref,)
        ids = db.query(sql + " ORDER BY id", params)
        with db.lock:
            return stamped(app, [_view(db.conn, r["id"]) for r in ids])

    @app.patch("/admin/shipments/{sid}")
    def set_status(sid: str, body: StatusIn):
        if body.status not in ("planned", "dispatched", "delivered", "cancelled"):
            raise ApiError(422, "bad_status")
        with db.tx() as conn:
            if conn.execute("SELECT 1 FROM shipments WHERE id=?", (sid,)).fetchone() is None:
                raise ApiError(404, "no_such_shipment")
            conn.execute("UPDATE shipments SET status=? WHERE id=?", (body.status, sid))
            return _view(conn, sid)

    return app

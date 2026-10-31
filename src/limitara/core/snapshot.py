"""Reads every stand-in the promise needs, stamps each read, re-reads stale parts once."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from limitara.calendar import parse_weekdays
from limitara.clock import IST, Clock, ist_date
from limitara.contracts import Stamped, Unknown
from limitara.core.clients import Clients
from limitara.core.config import DEFAULT, Config
from limitara.core.promise.model import CapRow, Lane, Schedule, Snapshot, StockRow
from limitara.freshness import check


class SnapshotBuilder:
    def __init__(self, clients: Clients, clock: Clock, config: Config = DEFAULT):
        self.clients, self.clock, self.config = clients, clock, config

    def _fresh(self, kind: str, read) -> Stamped:
        for attempt in (1, 2):
            stamped = read()
            verdict = check(kind, stamped.read_at, self.clock.now())
            if verdict.fresh:
                return stamped
            if attempt == 2 or not self.config.freshness_retry:
                raise Unknown(stamped.source, verdict.reason or "stale read")
        raise AssertionError("unreachable")

    def build(self, owner_refs: tuple[str, ...] = ()) -> Snapshot:
        c, cfg = self.clients, self.config
        now = self.clock.now()
        today = ist_date(now)
        horizon_end = today + timedelta(days=cfg.horizon_days - 1)

        items = self._fresh("price_list", lambda: c.erp.get("/items"))
        bom = self._fresh("price_list", lambda: c.erp.get("/bom"))
        locs = self._fresh("price_list", lambda: c.erp.get("/locations"))
        stock = self._fresh("stock", lambda: c.erp.get("/stock"))
        lines = self._fresh("capacity", lambda: c.production.get("/lines"))
        cap = self._fresh("capacity", lambda: c.production.get("/capacity", from_day=today.isoformat(), to_day=horizon_end.isoformat()))
        lanes = self._fresh("dispatch_schedule", lambda: c.dispatch.get("/lanes"))
        sched = self._fresh("dispatch_schedule", lambda: c.dispatch.get("/schedules"))
        hol = self._fresh("dispatch_schedule", lambda: c.dispatch.get("/holidays"))
        whs = self._fresh("dispatch_schedule", lambda: c.dispatch.get("/warehouses"))

        stock_rows = {(r["location_id"], r["item_id"]): StockRow(r["on_hand"], r["reserved"], r["counted_at"]) for r in stock.data}
        cap_rows: dict[tuple[str, date], CapRow] = {}
        line_plant: dict[str, str] = {}
        for r in cap.data:
            key = (r["line_id"], date.fromisoformat(r["day"]))
            if key in cap_rows:
                raise Unknown("production", f"duplicate capacity rows for {key[0]} on {key[1]}")
            cap_rows[key] = CapRow(r["total_units"], r["booked_units"], r["status"], r["note"])
            line_plant[r["line_id"]] = r["plant_id"]

        for ref in owner_refs:  # add back what this quote's own holds are keeping out of reach
            for res in self._fresh("stock", lambda ref=ref: c.erp.get("/reservations", ref_id=ref)).data:
                if res["status"] == "active":
                    for it in res["items"]:
                        k = (it["location_id"], it["item_id"])
                        if k in stock_rows:
                            row = stock_rows[k]
                            stock_rows[k] = StockRow(row.on_hand, max(0, row.reserved - it["qty"]), row.counted_at)
            for bk in self._fresh("capacity", lambda ref=ref: c.production.get("/bookings", ref_id=ref)).data:
                if bk["status"] == "active":
                    for d in bk["days"]:
                        k = (d["line_id"], date.fromisoformat(d["day"]))
                        if k in cap_rows:
                            row = cap_rows[k]
                            cap_rows[k] = CapRow(row.total, max(0, row.booked - d["units"]), row.status, row.note)

        bom_map: dict[str, tuple[str, int]] = {r["product_id"]: (r["material_id"], r["qty_per_unit"]) for r in bom.data}
        locations = {r["id"]: r["kind"] for r in locs.data}
        local = now.astimezone(IST)
        return Snapshot(
            now=now,
            today=today,
            now_ist_time=local.time().replace(microsecond=0),
            cost={r["id"]: Decimal(r["unit_cost"]) for r in items.data if r["kind"] == "finished"},
            bom=bom_map,
            stock=stock_rows,
            capacity=cap_rows,
            line_plant=line_plant,
            line_products={r["id"]: frozenset(r["products"]) for r in lines.data},
            lanes={(r["from_plant"], r["to_warehouse"]): Lane(r["transit_days"], Decimal(r["per_unit_charge"])) for r in lanes.data},
            schedules={
                (r["warehouse_id"], r["destination_city"]): Schedule(
                    parse_weekdays(r["weekdays"]), time.fromisoformat(r["cutoff_ist"]), r["transit_days"],
                    Decimal(r["base_charge"]), Decimal(r["per_unit_charge"]), r["carrier"])
                for r in sched.data
            },
            holidays=frozenset(date.fromisoformat(r["day"]) for r in hol.data),
            warehouses=frozenset(r["id"] for r in whs.data),
            plants=frozenset(k for k, v in locations.items() if v == "plant"),
            reads={"erp": stock.read_at, "production": cap.read_at, "dispatch": sched.read_at},
        )

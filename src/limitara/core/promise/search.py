"""Plan search. Pure: a snapshot and a request in, plans out.

A plan is a set of shipments, and each warehouse ships at most once (an extra shipment is an
extra base freight charge). Allocation is greedy over a shared ledger, so no unit, line-day or
kilo of steel is used twice. Greedy may offer a later date than the best possible, never an
impossible one; verify.py checks every answer independently.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from limitara.calendar import delivery_date, is_carrier_day, next_ship_date
from limitara.core.config import Config
from limitara.core.promise.model import Alloc, Plan, Shipment, Snapshot


@dataclass
class Ledger:
    stock: dict
    cap: dict
    rm: dict

    def copy(self) -> "Ledger":
        return Ledger(dict(self.stock), dict(self.cap), dict(self.rm))


def ledger_from(snap: Snapshot) -> Ledger:
    materials = {m for m, _ in snap.bom.values()}
    return Ledger(
        stock={k: v.available for k, v in snap.stock.items()},
        cap={k: v.free for k, v in snap.capacity.items()},
        rm={(loc, item): v.available for (loc, item), v in snap.stock.items() if loc in snap.plants and item in materials},
    )


def latest_ship(snap: Snapshot, warehouse: str, city: str, deadline: date) -> date | None:
    sched = snap.schedules.get((warehouse, city))
    if sched is None:
        return None
    day = deadline
    while day >= snap.today:
        if is_carrier_day(day, sched.weekdays, snap.holidays) and delivery_date(day, sched.transit, snap.holidays) <= deadline:
            return day
        day -= timedelta(days=1)
    return None


def candidates(snap: Snapshot, cfg: Config, product: str, w: str, city: str, latest: dict[str, date]) -> list[tuple]:
    """(priority, avail_from, unit_transfer_cost, warehouse, kind, location, line, day)"""
    sched = snap.schedules[(w, city)]
    out: list[tuple] = []
    stock_from = snap.today if snap.now_ist_time < sched.cutoff else snap.today + timedelta(days=1)
    out.append((0, stock_from, Decimal(0), w, "stock", w, None, None))
    for plant in sorted(snap.plants):
        lane = snap.lanes.get((plant, w))
        if lane is None:
            continue
        out.append((1, snap.today + timedelta(days=lane.transit + cfg.handling_days), lane.per_unit, w, "plant_stock", plant, None, None))
        if product not in snap.bom:
            continue  # no bill of materials: we cannot know the steel, so we do not plan production
        for line in sorted(snap.line_plant):
            if snap.line_plant[line] != plant or product not in snap.line_products.get(line, frozenset()):
                continue
            for offset in range(cfg.production_start_lag_days, cfg.horizon_days):
                day = snap.today + timedelta(days=offset)
                if (line, day) in snap.capacity:
                    avail = day + timedelta(days=1 + lane.transit + cfg.handling_days)
                    out.append((2, avail, lane.per_unit, w, "production", plant, line, day))
    return [c for c in out if c[1] <= latest[w]]


def _live(ledger: Ledger, snap: Snapshot, product: str, c: tuple) -> int:
    _, _, _, w, kind, loc, line, day = c
    if kind == "stock":
        return ledger.stock.get((w, product), 0)
    if kind == "plant_stock":
        return ledger.stock.get((loc, product), 0)
    material, per = snap.bom[product]
    return min(ledger.cap.get((line, day), 0), ledger.rm.get((loc, material), 0) // per)


def _take(ledger: Ledger, snap: Snapshot, product: str, c: tuple, qty: int) -> None:
    _, _, _, w, kind, loc, line, day = c
    if kind == "stock":
        ledger.stock[(w, product)] -= qty
    elif kind == "plant_stock":
        ledger.stock[(loc, product)] -= qty
    else:
        material, per = snap.bom[product]
        ledger.cap[(line, day)] -= qty
        ledger.rm[(loc, material)] -= qty * per


def fill(snap: Snapshot, cfg: Config, ledger: Ledger, origins: tuple[str, ...], deadline: date,
         lines: tuple[tuple[str, int], ...], city: str, require_full: bool = True):
    """Try to cover the lines using only these warehouses, every shipment delivering by the deadline."""
    latest = {w: latest_ship(snap, w, city, deadline) for w in origins}
    usable = [w for w in origins if latest[w] is not None]
    if require_full and len(usable) < len(origins):
        return None
    allocs: dict[str, list[Alloc]] = {w: [] for w in usable}
    remaining: dict[str, int] = {}
    for product, qty in sorted(lines, key=lambda x: (-x[1], x[0])):
        need = qty
        cands: list[tuple] = []
        for w in usable:
            cands += candidates(snap, cfg, product, w, city, latest)
        cands.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[5], str(c[6]), str(c[7])))
        for c in cands:
            if need == 0:
                break
            q = min(_live(ledger, snap, product, c), need)
            if q <= 0:
                continue
            _take(ledger, snap, product, c, q)
            allocs[c[3]].append(Alloc(c[4], product, q, c[5], c[1], c[6], c[7]))
            need -= q
        remaining[product] = need
    if require_full and (any(remaining.values()) or any(not allocs[w] for w in usable)):
        return None
    return {w: a for w, a in allocs.items() if a}, remaining


def build_plan(snap: Snapshot, allocs: dict[str, list[Alloc]], city: str, notes: tuple[str, ...] = ()) -> Plan:
    shipments = []
    for w in sorted(allocs):
        sched = snap.schedules[(w, city)]
        ship = next_ship_date(max(a.avail_from for a in allocs[w]), sched.weekdays, snap.holidays)
        units = sum(a.qty for a in allocs[w])
        transfer = sum((snap.lanes[(a.location, w)].per_unit * a.qty for a in allocs[w] if a.kind != "stock"), Decimal(0))
        shipments.append(Shipment(
            warehouse=w, ship_date=ship, delivery=delivery_date(ship, sched.transit, snap.holidays),
            allocs=tuple(allocs[w]), units=units, freight=sched.base + sched.per_unit * units, transfer_cost=transfer))
    return combine(shipments, notes)


def combine(shipments: list[Shipment], notes: tuple[str, ...] = ()) -> Plan:
    return Plan(
        shipments=tuple(shipments), delivery=max(s.delivery for s in shipments),
        freight=sum((s.freight for s in shipments), Decimal(0)),
        transfer_cost=sum((s.transfer_cost for s in shipments), Decimal(0)), notes=notes)


def search_plans(snap: Snapshot, cfg: Config, lines: tuple[tuple[str, int], ...], city: str,
                 d_start: date | None = None, ledger: Ledger | None = None) -> list[Plan]:
    served = sorted(w for w in snap.warehouses if (w, city) in snap.schedules)
    base = ledger or ledger_from(snap)
    first = d_start or snap.today + timedelta(days=1)
    last = snap.today + timedelta(days=cfg.horizon_days + 7)
    plans: list[Plan] = []
    for size in range(1, len(served) + 1):
        for origins in combinations(served, size):
            deadline = first
            while deadline <= last:
                trial = base.copy()
                result = fill(snap, cfg, trial, origins, deadline, lines, city)
                if result:
                    plans.append(build_plan(snap, result[0], city))
                    break
                deadline += timedelta(days=1)
    return plans


def part_now_plan(snap: Snapshot, cfg: Config, lines: tuple[tuple[str, int], ...], city: str, deadline: date) -> Plan | None:
    """The most that can arrive by the deadline, then the rest as a second, later shipment set."""
    served = tuple(sorted(w for w in snap.warehouses if (w, city) in snap.schedules))
    ledger = ledger_from(snap)
    result = fill(snap, cfg, ledger, served, deadline, lines, city, require_full=False)
    if not result or not result[0]:
        return None
    allocs, remaining = result
    first = build_plan(snap, allocs, city)
    left = tuple((p, q) for p, q in remaining.items() if q > 0)
    if not left:
        return first
    later = search_plans(snap, cfg, left, city, d_start=deadline + timedelta(days=1), ledger=ledger)
    if not later:
        return None
    rest = min(later, key=lambda p: (p.delivery, p.cost))
    done = sum(a.qty for s in first.shipments for a in s.allocs)
    note = f"{done} units arrive by {deadline.isoformat()}; the rest follows by {rest.delivery.isoformat()}"
    return combine(list(first.shipments) + list(rest.shipments), (note,))

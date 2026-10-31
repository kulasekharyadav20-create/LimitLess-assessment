"""The independent check. It recomputes every claim in a plan from the raw snapshot and does not
trust the search code. Empty list means the plan is true."""

from collections import Counter
from datetime import timedelta

from limitara.calendar import delivery_date, is_carrier_day, next_ship_date
from limitara.core.config import Config
from limitara.core.promise.model import Plan, Snapshot


def verify_plan(plan: Plan, snap: Snapshot, lines: tuple[tuple[str, int], ...], city: str, cfg: Config) -> list[str]:
    errs: list[str] = []
    covered: Counter = Counter()
    use_stock: Counter = Counter()
    use_cap: Counter = Counter()
    use_rm: Counter = Counter()

    for sh in plan.shipments:
        sched = snap.schedules.get((sh.warehouse, city))
        if sched is None:
            errs.append(f"{sh.warehouse} has no service to {city}")
            continue
        expected: list = []
        for a in sh.allocs:
            covered[a.product] += a.qty
            if a.qty <= 0:
                errs.append(f"non-positive quantity for {a.product}")
            if a.kind == "stock":
                if a.location != sh.warehouse:
                    errs.append(f"stock alloc at {a.location} in shipment from {sh.warehouse}")
                exp = snap.today if snap.now_ist_time < sched.cutoff else snap.today + timedelta(days=1)
                use_stock[(sh.warehouse, a.product)] += a.qty
            elif a.kind == "plant_stock":
                lane = snap.lanes.get((a.location, sh.warehouse))
                if lane is None:
                    errs.append(f"no lane {a.location} to {sh.warehouse}")
                    continue
                exp = snap.today + timedelta(days=lane.transit + cfg.handling_days)
                use_stock[(a.location, a.product)] += a.qty
            elif a.kind == "production":
                lane = snap.lanes.get((a.location, sh.warehouse))
                if lane is None or a.line is None or a.day is None:
                    errs.append(f"production alloc without a lane, line or day: {a}")
                    continue
                row = snap.capacity.get((a.line, a.day))
                if row is None or row.status != "open":
                    errs.append(f"{a.line} is not open on {a.day}")
                if a.day < snap.today + timedelta(days=cfg.production_start_lag_days):
                    errs.append(f"production on {a.day} starts too early")
                if a.product not in snap.line_products.get(a.line, frozenset()):
                    errs.append(f"{a.line} cannot make {a.product}")
                if snap.line_plant.get(a.line) != a.location:
                    errs.append(f"{a.line} is not at {a.location}")
                exp = a.day + timedelta(days=1 + lane.transit + cfg.handling_days)
                use_cap[(a.line, a.day)] += a.qty
                material = snap.bom.get(a.product)
                if material is None:
                    errs.append(f"no bill of materials for {a.product}")
                else:
                    use_rm[(a.location, material[0])] += a.qty * material[1]
            else:
                errs.append(f"unknown allocation kind {a.kind}")
                continue
            if a.avail_from != exp:
                errs.append(f"{a.product} from {a.location}: ready date {a.avail_from} should be {exp}")
            expected.append(exp)
        if not expected:
            errs.append(f"shipment from {sh.warehouse} carries nothing verifiable")
            continue
        earliest = next_ship_date(max(expected), sched.weekdays, snap.holidays)
        if earliest is None or sh.ship_date < earliest:
            errs.append(f"{sh.warehouse} ships {sh.ship_date} before goods can leave ({earliest})")
        if not is_carrier_day(sh.ship_date, sched.weekdays, snap.holidays):
            errs.append(f"{sh.warehouse} has no carrier on {sh.ship_date} (weekday or holiday)")
        if sh.ship_date == snap.today and not snap.now_ist_time < sched.cutoff:
            errs.append(f"{sh.warehouse} cut-off {sched.cutoff} has passed today")
        if sh.delivery != delivery_date(sh.ship_date, sched.transit, snap.holidays):
            errs.append(f"{sh.warehouse} delivery date {sh.delivery} is wrong")
        units = sum(a.qty for a in sh.allocs)
        if sh.units != units:
            errs.append(f"{sh.warehouse} units {sh.units} differ from allocations {units}")
        if sh.freight != sched.base + sched.per_unit * units:
            errs.append(f"{sh.warehouse} freight {sh.freight} is wrong")

    if dict(covered) != dict(lines):
        errs.append(f"coverage: plan carries {dict(covered)} but the request is {dict(lines)}")
    for (loc, product), q in use_stock.items():
        row = snap.stock.get((loc, product))
        if row is None or q > row.available:
            errs.append(f"stock {product} at {loc}: plan uses {q}, available {0 if row is None else row.available}")
    for (line, day), q in use_cap.items():
        row = snap.capacity.get((line, day))
        if row is None or q > row.free:
            errs.append(f"capacity {line} on {day}: plan uses {q}, free {0 if row is None else row.free}")
    for (plant, material), q in use_rm.items():
        row = snap.stock.get((plant, material))
        if row is None or q > row.available:
            errs.append(f"steel {material} at {plant}: plan uses {q}, available {0 if row is None else row.available}")
    if plan.delivery != max((s.delivery for s in plan.shipments), default=None):
        errs.append("plan delivery is not the latest shipment delivery")
    return errs

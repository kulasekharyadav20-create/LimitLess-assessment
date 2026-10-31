"""The basis of a plan: every record it relied on, with values and read time (principle 2)."""

from datetime import date, timedelta

from limitara.core.config import Config
from limitara.core.promise.model import Plan, Snapshot


def basis(plan: Plan, snap: Snapshot, city: str, cfg: Config) -> list[dict]:
    out: list[dict] = []
    erp_at = snap.reads.get("erp").isoformat() if snap.reads.get("erp") else None
    prod_at = snap.reads.get("production").isoformat() if snap.reads.get("production") else None
    disp_at = snap.reads.get("dispatch").isoformat() if snap.reads.get("dispatch") else None
    seen: set = set()
    for sh in plan.shipments:
        sched = snap.schedules[(sh.warehouse, city)]
        out.append({"source": "dispatch", "record": f"schedule:{sh.warehouse}:{city}", "read_at": disp_at,
                    "values": {"carrier": sched.carrier, "weekdays": sorted(sched.weekdays), "cutoff": sched.cutoff.isoformat(),
                               "transit_days": sched.transit, "ship_date": sh.ship_date.isoformat(),
                               "delivery": sh.delivery.isoformat(), "freight": str(sh.freight)}})
        for a in sh.allocs:
            if a.kind in ("stock", "plant_stock"):
                loc = a.location
                key = ("stock", loc, a.product)
                row = snap.stock[(loc, a.product)]
                if key not in seen:
                    seen.add(key)
                    out.append({"source": "erp", "record": f"stock:{loc}:{a.product}", "read_at": erp_at,
                                "values": {"on_hand": row.on_hand, "reserved": row.reserved, "available": row.available,
                                           "counted_at": row.counted_at,
                                           "count_old": _count_old(row.counted_at, snap.today, cfg)}})
            else:
                key = ("cap", a.line, a.day)
                if key not in seen:
                    seen.add(key)
                    c = snap.capacity[(a.line, a.day)]
                    out.append({"source": "production", "record": f"capacity:{a.line}:{a.day.isoformat()}", "read_at": prod_at,
                                "values": {"total": c.total, "booked": c.booked, "free": c.free, "used_by_plan": a.qty}})
                material, _ = snap.bom[a.product]
                key = ("rm", a.location, material)
                if key not in seen:
                    seen.add(key)
                    r = snap.stock[(a.location, material)]
                    out.append({"source": "erp", "record": f"stock:{a.location}:{material}", "read_at": erp_at,
                                "values": {"on_hand": r.on_hand, "reserved": r.reserved, "available": r.available}})
    out.append({"source": "dispatch", "record": "holidays", "read_at": disp_at,
                "values": {"holidays": sorted(d.isoformat() for d in snap.holidays)}})
    return out


def _count_old(counted_at: str, today: date, cfg: Config) -> bool:
    return date.fromisoformat(counted_at) < today - timedelta(days=cfg.count_old_days)


def old_counts(plan: Plan, snap: Snapshot, cfg: Config) -> list[str]:
    """Stock rows the plan uses whose last count is older than the limit."""
    out = []
    for sh in plan.shipments:
        for a in sh.allocs:
            if a.kind in ("stock", "plant_stock"):
                row = snap.stock[(a.location, a.product)]
                if _count_old(row.counted_at, snap.today, cfg):
                    out.append(f"{a.product} at {a.location} was last counted {row.counted_at}")
    return sorted(set(out))

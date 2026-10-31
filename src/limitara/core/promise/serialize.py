"""Plans to and from plain JSON (for core.db and the screens)."""

from datetime import date
from decimal import Decimal

from limitara.core.promise.model import Alloc, Plan, Shipment


def alloc_to_dict(a: Alloc) -> dict:
    return {"kind": a.kind, "product": a.product, "qty": a.qty, "location": a.location,
            "avail_from": a.avail_from.isoformat(), "line": a.line, "day": a.day.isoformat() if a.day else None}


def shipment_to_dict(s: Shipment) -> dict:
    return {"warehouse": s.warehouse, "ship_date": s.ship_date.isoformat(), "delivery": s.delivery.isoformat(),
            "units": s.units, "freight": str(s.freight), "transfer_cost": str(s.transfer_cost),
            "allocs": [alloc_to_dict(a) for a in s.allocs]}


def plan_to_dict(p: Plan) -> dict:
    return {"shipments": [shipment_to_dict(s) for s in p.shipments], "delivery": p.delivery.isoformat(),
            "freight": str(p.freight), "transfer_cost": str(p.transfer_cost), "notes": list(p.notes)}


def plan_from_dict(d: dict) -> Plan:
    shipments = tuple(
        Shipment(
            warehouse=s["warehouse"], ship_date=date.fromisoformat(s["ship_date"]), delivery=date.fromisoformat(s["delivery"]),
            units=s["units"], freight=Decimal(s["freight"]), transfer_cost=Decimal(s["transfer_cost"]),
            allocs=tuple(
                Alloc(a["kind"], a["product"], a["qty"], a["location"], date.fromisoformat(a["avail_from"]), a["line"],
                      date.fromisoformat(a["day"]) if a["day"] else None)
                for a in s["allocs"]))
        for s in d["shipments"])
    return Plan(shipments, date.fromisoformat(d["delivery"]), Decimal(d["freight"]), Decimal(d["transfer_cost"]), tuple(d["notes"]))

"""Change one thing, run the same pure function again, show what changed and what did not. Nothing is written."""

from dataclasses import replace
from datetime import date

from limitara.core.config import Config
from limitara.core.promise import build_promise
from limitara.core.promise.model import CapRow, Request, Snapshot, StockRow
from limitara.core.promise.serialize import plan_to_dict


def apply_change(snap: Snapshot, change: dict) -> Snapshot:
    kind = change["kind"]
    if kind == "line_down":
        a, b = date.fromisoformat(change["from"]), date.fromisoformat(change["to"])
        cap = {k: (CapRow(0, 0, "down", "what-if") if k[0] == change["line"] and a <= k[1] <= b else v) for k, v in snap.capacity.items()}
        return replace(snap, capacity=cap)
    if kind == "plant_down":
        a, b = date.fromisoformat(change["from"]), date.fromisoformat(change["to"])
        lines = {l for l, p in snap.line_plant.items() if p == change["plant"]}
        cap = {k: (CapRow(0, 0, "down", "what-if") if k[0] in lines and a <= k[1] <= b else v) for k, v in snap.capacity.items()}
        return replace(snap, capacity=cap)
    if kind == "stock_set":
        key = (change["location"], change["item"])
        old = snap.stock[key]
        stock = dict(snap.stock)
        stock[key] = StockRow(change["on_hand"], min(old.reserved, change["on_hand"]), old.counted_at)
        return replace(snap, stock=stock)
    raise ValueError(f"unknown change {kind!r}")


def summary(result) -> dict:
    plan = result.offered or next(iter(result.alternatives.values()), None)
    return {"status": result.status, "needs_consent": result.needs_consent,
            "plan": plan_to_dict(plan) if plan else None,
            "delivery": plan.delivery.isoformat() if plan else None, "freight": str(plan.freight) if plan else None,
            "parts": plan.parts if plan else None, "alternatives": sorted(result.alternatives)}


def what_if(snap: Snapshot, req: Request, change: dict, cfg: Config) -> dict:
    before = summary(build_promise(snap, req, cfg))
    after = summary(build_promise(apply_change(snap, change), req, cfg))
    changed = {k: {"before": before[k], "after": after[k]} for k in ("status", "delivery", "freight", "parts", "alternatives", "needs_consent") if before[k] != after[k]}
    return {"change": change, "before": before, "after": after, "what_changed": changed,
            "what_did_not": sorted(k for k in ("status", "delivery", "freight", "parts", "alternatives", "needs_consent") if before[k] == after[k])}

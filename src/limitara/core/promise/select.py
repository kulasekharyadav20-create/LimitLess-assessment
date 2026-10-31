"""Turn candidate plans into an honest offer. Never labels an unachievable date as achievable."""

from dataclasses import dataclass, field
from decimal import Decimal

from limitara.core.config import Config
from limitara.core.promise.model import Plan, Request, Snapshot
from limitara.core.promise.search import part_now_plan, search_plans

FULL = "FULL_ON_DATE"
ALTERNATIVES = "ALTERNATIVES_ONLY"
NOTHING = "NONE_WITHIN_HORIZON"


@dataclass(frozen=True)
class PromiseResult:
    status: str
    offered: Plan | None = None
    needs_consent: bool = False
    options: tuple[Plan, ...] = ()
    alternatives: dict[str, Plan] = field(default_factory=dict)
    best_single: Plan | None = None
    freight_delta: Decimal | None = None
    notes: tuple[str, ...] = ()


def _key(p: Plan):
    return (p.cost, p.delivery, p.parts)


def build_promise(snap: Snapshot, req: Request, cfg: Config) -> PromiseResult:
    plans = search_plans(snap, cfg, req.lines, req.city)
    singles = [p for p in plans if p.parts == 1]
    splits = [p for p in plans if p.parts > 1]
    usable = singles if req.partial == "not_allowed" else plans
    on_time = [p for p in usable if p.delivery <= req.deliver_by]
    best_single = min(singles, key=lambda p: (p.cost, p.delivery)) if singles else None

    if on_time:
        consent = False
        if req.partial == "ask":
            single_on_time = [p for p in on_time if p.parts == 1]
            if single_on_time:
                offered = min(single_on_time, key=_key)
            else:
                offered, consent = min(on_time, key=_key), True
        else:
            offered = min(on_time, key=_key)
        delta = offered.cost - best_single.cost if best_single and offered.parts > 1 else None
        options = tuple(sorted((p for p in on_time if p is not offered), key=_key))
        return PromiseResult(FULL, offered, consent, options, {}, best_single, delta)

    alts: dict[str, Plan] = {}
    if singles:
        alts["A_single_later"] = min(singles, key=lambda p: (p.delivery, p.cost))
    if req.partial != "not_allowed":
        if splits:
            alts["B_split_earliest"] = min(splits, key=lambda p: (p.delivery, p.cost))
        part = part_now_plan(snap, cfg, req.lines, req.city, req.deliver_by)
        if part is not None and part.parts > 1:
            alts["C_part_now"] = part
    if not alts:
        return PromiseResult(NOTHING, notes=("no plan covers the request within the planning horizon",))
    return PromiseResult(ALTERNATIVES, None, req.partial == "ask", (), alts, best_single, None,
                         (f"requested date {req.deliver_by.isoformat()} cannot be met",))

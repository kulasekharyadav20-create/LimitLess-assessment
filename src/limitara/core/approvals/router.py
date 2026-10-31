"""Who must approve. Pure: flags and authorities in, required roles out."""

from dataclasses import dataclass
from decimal import Decimal

from limitara.core.pricing.flags import Flag

MANAGER, HEAD, FINANCE = "sales_manager", "sales_head", "finance"


@dataclass(frozen=True)
class Routing:
    roles: tuple[str, ...]
    unapprovable: bool = False
    reasons: tuple[str, ...] = ()


def route_approvals(flags: list[Flag] | tuple[Flag, ...], authorities: dict[str, Decimal]) -> Routing:
    """authorities maps role -> the lowest margin (percent) that role may approve."""
    roles: set[str] = {MANAGER}
    reasons: list[str] = []
    margins = [f.margin_pct for f in flags if f.margin_pct is not None]
    unapprovable = False
    if margins:
        lowest = min(margins)
        if lowest >= authorities[MANAGER]:
            reasons.append(f"lowest margin {lowest:.2f}% is within the sales manager's authority ({authorities[MANAGER]}%)")
        elif lowest >= authorities[HEAD]:
            roles.add(HEAD)
            reasons.append(f"lowest margin {lowest:.2f}% is below the manager's {authorities[MANAGER]}%: sales head required")
        else:
            unapprovable = True
            reasons.append(f"lowest margin {lowest:.2f}% is below every approver's authority; the quote must be repriced")
    for f in flags:
        if f.severity == "needs_role" and f.role:
            roles.add(f.role)
            reasons.append(f"{f.code} needs {f.role}")
    if HEAD in roles:
        roles.discard(MANAGER)  # a head satisfies the manager requirement
    ordered = tuple(r for r in (MANAGER, HEAD, FINANCE) if r in roles)
    return Routing(ordered, unapprovable, tuple(reasons))


def can_satisfy(approver_role: str, required_role: str) -> bool:
    if approver_role == required_role:
        return True
    return required_role == MANAGER and approver_role == HEAD

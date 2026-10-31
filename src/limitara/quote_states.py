"""The quote version state machine. ERP enforces it; Core uses the same table."""

ALLOWED: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_approval", "withdrawn"}),
    "pending_approval": frozenset({"approved", "rejected", "withdrawn"}),
    "approved": frozenset({"sent", "needs_replan", "withdrawn"}),
    "needs_replan": frozenset({"withdrawn"}),
    "sent": frozenset({"accepted", "declined", "expired", "superseded"}),
}
FINAL = frozenset({"accepted", "declined", "expired", "superseded", "rejected", "withdrawn"})
ALL_STATES = frozenset(ALLOWED) | FINAL


class IllegalTransition(ValueError):
    pass


def next_state(current: str, to: str) -> str:
    if to not in ALL_STATES:
        raise IllegalTransition(f"unknown state {to!r}")
    if to not in ALLOWED.get(current, frozenset()):
        raise IllegalTransition(f"cannot move a version from {current} to {to}")
    return to

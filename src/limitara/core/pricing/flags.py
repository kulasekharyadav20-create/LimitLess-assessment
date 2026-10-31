"""Flags: every reason a person must look. Pure data."""

from dataclasses import dataclass, field
from decimal import Decimal

BLOCK, NEEDS_ROLE, INFO = "block", "needs_role", "info"

# code -> (severity, default role that resolves it)
CATALOGUE: dict[str, tuple[str, str | None]] = {
    "CANNOT_PRICE": (BLOCK, "rep"),
    "UNKNOWN_DATA": (BLOCK, "rep"),
    "DATE_AMBIGUOUS": (BLOCK, "rep"),
    "CONFLICTING_SOURCES": (BLOCK, "rep"),
    "SPLIT_NEEDS_CONSENT": (BLOCK, "rep"),
    "BELOW_FLOOR": (NEEDS_ROLE, None),            # role chosen by margin
    "TERMS_BREACH_FLOOR": (NEEDS_ROLE, None),     # role chosen by margin
    "TERMS_EXPIRED": (NEEDS_ROLE, "sales_manager"),
    "AGREED_LIST_EXPIRED": (NEEDS_ROLE, "sales_manager"),
    "VALIDITY_SHORTENED": (NEEDS_ROLE, "sales_manager"),
    "CREDIT_HOLD": (NEEDS_ROLE, "finance"),
    "CREDIT_HEADROOM_LOW": (NEEDS_ROLE, "finance"),
    "COUNT_OLD": (NEEDS_ROLE, "sales_manager"),
    "DATE_NOT_MET": (INFO, None),
    "OPEN_DISPUTE": (INFO, None),
    "CUSTOMER_REQUESTS": (INFO, None),
    "POSSIBLE_DUPLICATE": (INFO, None),
    "NO_TERMS": (INFO, None),
}


@dataclass(frozen=True)
class Flag:
    code: str
    detail: str
    severity: str = ""
    role: str | None = None
    margin_pct: Decimal | None = None
    provenance: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self):
        sev, role = CATALOGUE[self.code]
        object.__setattr__(self, "severity", sev)
        if self.role is None:
            object.__setattr__(self, "role", role)


def money(d: Decimal) -> str:
    return f"Rs {d:,.2f}"

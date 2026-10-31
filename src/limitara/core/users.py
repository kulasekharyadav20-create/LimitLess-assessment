"""Who is asking. Sign-in is a stub (choose a person); what each person may do is real."""

from dataclasses import dataclass

from limitara.contracts import Unknown
from limitara.core.context import Ctx

STAFF_ROLES = ("rep", "sales_manager", "sales_head", "finance")


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str                       # customer | rep | sales_manager | sales_head | finance
    account_id: str | None = None   # set for customers

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def is_approver(self) -> bool:
        return self.role in ("sales_manager", "sales_head", "finance")


def resolve_user(ctx: Ctx, user_id: str) -> User | None:
    try:
        if user_id.startswith("CT-"):
            for acc in ctx.clients.crm.get("/accounts").data:
                for c in ctx.clients.crm.get(f"/accounts/{acc['id']}/contacts").data:
                    if c["id"] == user_id:
                        return User(c["id"], c["name"], "customer", acc["id"])
            return None
        row = ctx.clients.crm.get(f"/staff/{user_id}").data
        return User(row["id"], row["name"], row["role"])
    except Unknown:
        return None


def all_users(ctx: Ctx) -> list[User]:
    users = [User(s["id"], s["name"], s["role"]) for s in ctx.clients.crm.get("/staff").data]
    for acc in ctx.clients.crm.get("/accounts").data:
        for c in ctx.clients.crm.get(f"/accounts/{acc['id']}/contacts").data:
            users.append(User(c["id"], f"{c['name']} ({acc['name']})", "customer", acc["id"]))
    return users

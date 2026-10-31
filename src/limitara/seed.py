"""Rebuild every stand-in database from scratch. Run with `uv run seed`.

Dates are relative to the run date (in IST) so capacity and validity windows are
always current; pin LIMITARA_FIXED_NOW to reproduce an exact day.
"""

from datetime import date, datetime
from importlib import import_module

from limitara.clock import clock_from_env, ist_date, to_wire
from limitara.db import connect, db_path, reset

STAND_INS = ("production", "erp", "dispatch", "crm", "mailbox")


def seed_all(today: date, now: datetime) -> dict[str, str]:
    paths = {}
    for name in STAND_INS:
        path = db_path(name)
        reset(path)
        conn = connect(path)
        import_module(f"limitara.standins.{name}.store").seed(conn, today, to_wire(now))
        conn.close()
        paths[name] = str(path)
    return paths


def main() -> None:
    now = clock_from_env().now()
    for name, path in seed_all(ist_date(now), now).items():
        print(f"seeded {name:<11} -> {path}")

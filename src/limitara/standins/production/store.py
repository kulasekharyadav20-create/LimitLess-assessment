"""Production stand-in data: plants, lines, daily capacity, work orders.

Seeded traps (each one is asserted in tests/test_seed_production.py):
- Plants differ in size: Aurangabad is large, Coimbatore is small and heavily booked.
- Line A1 is down for two days (zero capacity, with a reason).
- Sundays are explicitly closed; days beyond the 30-day horizon have no row at all,
  which means "unknown", not "zero".
- WO-1001 was promised for today+5 but its forecast finish has slipped to today+7.
- Capacity is in units/day regardless of product (no changeover time modelled).
"""

import random
import sqlite3
from datetime import date, timedelta

HORIZON_DAYS = 30

SCHEMA = """
CREATE TABLE plants(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL
);
CREATE TABLE lines(
    id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(id), name TEXT NOT NULL
);
CREATE TABLE line_products(
    line_id TEXT NOT NULL REFERENCES lines(id), product_id TEXT NOT NULL,
    PRIMARY KEY (line_id, product_id)
);
CREATE TABLE capacity(
    line_id TEXT NOT NULL REFERENCES lines(id),
    day TEXT NOT NULL,
    total_units INTEGER NOT NULL CHECK (total_units >= 0),
    booked_units INTEGER NOT NULL CHECK (booked_units >= 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'down')),
    note TEXT,
    PRIMARY KEY (line_id, day),
    CHECK (booked_units <= total_units)
);
CREATE TABLE work_orders(
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    plant_id TEXT NOT NULL REFERENCES plants(id),
    line_id TEXT NOT NULL REFERENCES lines(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    completed_qty INTEGER NOT NULL CHECK (completed_qty >= 0),
    state TEXT NOT NULL CHECK (state IN ('planned', 'in_progress', 'late', 'done', 'cancelled')),
    promised_date TEXT NOT NULL,
    forecast_finish TEXT NOT NULL,
    customer_id TEXT,
    sales_order_ref TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    booking_id TEXT
);
CREATE TABLE bookings(
    id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('soft', 'hard')),
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired')),
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE booking_days(
    booking_id TEXT NOT NULL REFERENCES bookings(id),
    line_id TEXT NOT NULL,
    day TEXT NOT NULL,
    product_id TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0)
);
"""

PLANTS = [
    ("PLT-A", "Aurangabad Plant", "Aurangabad"),
    ("PLT-B", "Coimbatore Plant", "Coimbatore"),
]

# line id, plant, name, products, units per working day, (min, max) share already booked
LINES = [
    ("A1", "PLT-A", "Aurangabad Line 1", ["FL-50", "FL-80"], 150, (0.40, 0.80)),
    ("A2", "PLT-A", "Aurangabad Line 2", ["FL-50"], 100, (0.20, 0.60)),
    ("B1", "PLT-B", "Coimbatore Line 1", ["FL-50", "FL-80"], 80, (0.60, 0.95)),
]

LINE_A1_DOWN_OFFSETS = (3, 4)


def seed(conn: sqlite3.Connection, today: date, updated_at: str) -> None:
    rng = random.Random(20261031)  # fixed: messy but identical on every run
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO plants VALUES (?,?,?)", PLANTS)

    for line_id, plant_id, name, products, per_day, (lo, hi) in LINES:
        conn.execute("INSERT INTO lines VALUES (?,?,?)", (line_id, plant_id, name))
        conn.executemany(
            "INSERT INTO line_products VALUES (?,?)", [(line_id, p) for p in products]
        )
        for offset in range(HORIZON_DAYS):
            day = today + timedelta(days=offset)
            if day.weekday() == 6:
                row = (line_id, day.isoformat(), 0, 0, "closed", "Sunday")
            elif line_id == "A1" and offset in LINE_A1_DOWN_OFFSETS:
                row = (line_id, day.isoformat(), 0, 0, "down", "Press 2 gearbox repair")
            else:
                booked = round(per_day * rng.uniform(lo, hi))
                row = (line_id, day.isoformat(), per_day, booked, "open", None)
            conn.execute("INSERT INTO capacity VALUES (?,?,?,?,?,?)", row)

    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    conn.executemany(
        "INSERT INTO work_orders(id, product_id, plant_id, line_id, qty, completed_qty, state, promised_date,"
        " forecast_finish, customer_id, sales_order_ref, note, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("WO-1001", "FL-50", "PLT-A", "A1", 300, 120, "late", d(5), d(7), "CUST-004",
             "SO-5530", "Slipped: Line A1 down, gearbox repair", updated_at),
            ("WO-1002", "FL-80", "PLT-B", "B1", 200, 90, "in_progress", d(6), d(6), "CUST-002",
             "SO-5541", None, updated_at),
            ("WO-1003", "FL-50", "PLT-A", "A2", 180, 0, "planned", d(2), d(2), "CUST-005",
             "SO-5544", None, updated_at),
            ("WO-0990", "FL-50", "PLT-A", "A2", 250, 250, "done", d(-2), d(-3), "CUST-006",
             "SO-5498", None, updated_at),
        ],
    )

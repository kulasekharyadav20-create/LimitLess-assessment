"""Dispatch stand-in data: carrier schedules, cut-offs, transfer lanes, shipments.

Freight for one shipment = base_charge + per_unit_charge * units. The base charge is
paid per shipment, which is why splitting an order costs extra.

Seeded traps (asserted in tests/test_seed_dispatch.py):
- Each warehouse reaches Pune on different weekdays, cut-offs and transit times.
- A festival holiday at today+8: nothing leaves any warehouse that day.
- Last quarter's order SO-Q3-0412 shipped in two parts, so the customer paid two
  base charges (the open freight dispute).
- No schedule row for a warehouse/city pair means "no service", not "free".
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

SCHEMA = """
CREATE TABLE warehouses(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL
);
CREATE TABLE transfer_lanes(
    from_plant TEXT NOT NULL,
    to_warehouse TEXT NOT NULL REFERENCES warehouses(id),
    transit_days INTEGER NOT NULL CHECK (transit_days >= 0),
    per_unit_charge TEXT NOT NULL,
    PRIMARY KEY (from_plant, to_warehouse)
);
CREATE TABLE schedules(
    warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
    destination_city TEXT NOT NULL,
    carrier TEXT NOT NULL,
    weekdays TEXT NOT NULL,
    cutoff_ist TEXT NOT NULL,
    transit_days INTEGER NOT NULL CHECK (transit_days >= 1),
    base_charge TEXT NOT NULL,
    per_unit_charge TEXT NOT NULL,
    PRIMARY KEY (warehouse_id, destination_city)
);
CREATE TABLE holidays(
    day TEXT PRIMARY KEY, note TEXT NOT NULL
);
CREATE TABLE shipments(
    id TEXT PRIMARY KEY,
    order_ref TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('customer', 'transfer')),
    from_location TEXT NOT NULL,
    destination TEXT NOT NULL,
    carrier TEXT,
    ship_date TEXT NOT NULL,
    delivery_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('planned', 'dispatched', 'delivered', 'cancelled')),
    freight_charge TEXT NOT NULL
);
CREATE TABLE shipment_lines(
    shipment_id TEXT NOT NULL REFERENCES shipments(id),
    product_id TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0)
);
"""

WAREHOUSES = [
    ("WH-BHW", "Bhiwandi Warehouse", "Bhiwandi"),
    ("WH-CHN", "Chennai Warehouse", "Chennai"),
    ("WH-GGN", "Gurgaon Warehouse", "Gurgaon"),
]

# plant, warehouse, transit days, per-unit charge
TRANSFER_LANES = [
    ("PLT-A", "WH-BHW", 1, "8.00"),
    ("PLT-A", "WH-GGN", 3, "14.00"),
    ("PLT-A", "WH-CHN", 3, "14.00"),
    ("PLT-B", "WH-CHN", 1, "7.00"),
    ("PLT-B", "WH-BHW", 3, "14.00"),
    ("PLT-B", "WH-GGN", 4, "18.00"),
]

# warehouse, city, carrier, weekdays (0=Mon), cut-off IST, transit, base, per unit
SCHEDULES = [
    ("WH-BHW", "Pune", "SwiftFreight", "0,1,2,3,4,5", "16:00", 1, "4500.00", "6.00"),
    ("WH-CHN", "Pune", "SouthLine", "1,3,5", "14:00", 2, "7200.00", "7.50"),
    ("WH-GGN", "Pune", "NorthStar", "0,2,4", "12:00", 3, "9800.00", "9.00"),
    ("WH-BHW", "Mumbai", "SwiftFreight", "0,1,2,3,4,5", "16:00", 1, "3200.00", "5.00"),
    ("WH-BHW", "Ahmedabad", "SwiftFreight", "0,2,4", "16:00", 2, "6100.00", "7.00"),
    ("WH-BHW", "Nagpur", "SwiftFreight", "1,3,5", "16:00", 2, "6800.00", "7.50"),
    ("WH-CHN", "Hyderabad", "SouthLine", "0,2,4", "14:00", 2, "5600.00", "6.50"),
    ("WH-GGN", "Delhi", "NorthStar", "0,1,2,3,4,5", "12:00", 1, "3900.00", "5.50"),
    ("WH-GGN", "Ahmedabad", "NorthStar", "1,4", "12:00", 3, "8400.00", "8.50"),
]


def freight(base: str, per_unit: str, units: int) -> str:
    return str(Decimal(base) + Decimal(per_unit) * units)


def seed(conn: sqlite3.Connection, today: date, now_iso: str) -> None:
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO warehouses VALUES (?,?,?)", WAREHOUSES)
    conn.executemany("INSERT INTO transfer_lanes VALUES (?,?,?,?)", TRANSFER_LANES)
    conn.executemany("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?)", SCHEDULES)
    conn.execute(
        "INSERT INTO holidays VALUES (?,?)",
        ((today + timedelta(days=8)).isoformat(), "Festival holiday: no dispatch"),
    )

    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    pune = {(s[0]): s for s in SCHEDULES if s[1] == "Pune"}
    shipments = [
        # id, order, customer, from, destination, carrier, ship, delivery, status, units, product
        ("SHP-2201", "SO-Q3-0412", "CUST-001", "WH-BHW", "Pune", "SwiftFreight", d(-75), d(-74), "delivered", 180, "FL-50"),
        ("SHP-2202", "SO-Q3-0412", "CUST-001", "WH-CHN", "Pune", "SouthLine", d(-74), d(-72), "delivered", 120, "FL-50"),
    ]
    rows, lines = [], []
    for sid, order, cust, wh, city, carrier, ship, deliv, status, units, product in shipments:
        charge = freight(pune[wh][6], pune[wh][7], units)
        rows.append((sid, order, cust, "customer", wh, city, carrier, ship, deliv, status, charge))
        lines.append((sid, product, units))
    extra = [
        ("SHP-2310", "SO-5498", "CUST-006", "WH-BHW", "Nagpur", "SwiftFreight", d(-1), d(1), "dispatched", 250, "FL-50", "6800.00", "7.50"),
        ("SHP-2311", "SO-5544", "CUST-005", "WH-GGN", "Delhi", "NorthStar", d(3), d(4), "planned", 180, "FL-50", "3900.00", "5.50"),
    ]
    for sid, order, cust, wh, city, carrier, ship, deliv, status, units, product, base, per in extra:
        rows.append((sid, order, cust, "customer", wh, city, carrier, ship, deliv, status, freight(base, per, units)))
        lines.append((sid, product, units))
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO shipment_lines VALUES (?,?,?)", lines)

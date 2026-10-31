"""ERP stand-in data: items, bill of materials, stock by location, price lists.

Quotations and sales orders are added with the quote-lifecycle stage, once we have
decided where they live.

Seeded traps (asserted in tests/test_seed_erp.py):
- Reserved stock: available = on_hand - reserved, and several rows are mostly reserved.
- FL-80 is in stock only as 60 + 40 + 20 across three warehouses: 120 exactly, no single
  warehouse can ship the 120 the enquiry asks for.
- Chennai finished stock was last counted 9 days ago (the record itself is old).
- Plant B steel for FL-50 is mostly reserved; Plant A steel for FL-80 is tight.
- Q3 price list is expired; a future list exists; one customer-specific list expires
  in two days, before a "valid till Friday" quote would.
- Money is TEXT.
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

SCHEMA = """
CREATE TABLE items(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('finished', 'raw')),
    unit_cost TEXT NOT NULL
);
CREATE TABLE bom(
    product_id TEXT NOT NULL REFERENCES items(id),
    material_id TEXT NOT NULL REFERENCES items(id),
    qty_per_unit INTEGER NOT NULL CHECK (qty_per_unit > 0),
    PRIMARY KEY (product_id, material_id)
);
CREATE TABLE locations(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('plant', 'warehouse')),
    city TEXT NOT NULL
);
CREATE TABLE stock(
    location_id TEXT NOT NULL REFERENCES locations(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL CHECK (reserved >= 0),
    counted_at TEXT NOT NULL,
    PRIMARY KEY (location_id, item_id),
    CHECK (reserved <= on_hand)
);
CREATE TABLE price_lists(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    customer_id TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR'
);
CREATE TABLE price_list_items(
    price_list_id TEXT NOT NULL REFERENCES price_lists(id),
    product_id TEXT NOT NULL REFERENCES items(id),
    unit_price TEXT NOT NULL,
    PRIMARY KEY (price_list_id, product_id)
);
"""

SCHEMA_DOCS = """
CREATE TABLE reservations(
    id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('soft', 'hard')),
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired')),
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE reservation_items(
    reservation_id TEXT NOT NULL REFERENCES reservations(id),
    location_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0)
);
CREATE TABLE quotations(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    case_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE quotation_versions(
    quotation_id TEXT NOT NULL REFERENCES quotations(id),
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    requested_delivery_date TEXT,
    valid_until TEXT NOT NULL,
    interpretation_note TEXT,
    total_ex_tax TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (quotation_id, version)
);
CREATE UNIQUE INDEX one_sent_version ON quotation_versions(quotation_id) WHERE status = 'sent';
CREATE TABLE quotation_lines(
    quotation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    list_price TEXT NOT NULL,
    discount_pct TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    price_list_id TEXT,
    promised_delivery_date TEXT,
    PRIMARY KEY (quotation_id, version, line_no)
);
CREATE TABLE sales_orders(
    id TEXT PRIMARY KEY,
    quotation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('created', 'confirmed', 'cancelled')),
    created_at TEXT NOT NULL,
    UNIQUE (quotation_id, version)
);
CREATE TABLE sales_order_lines(
    order_id TEXT NOT NULL REFERENCES sales_orders(id),
    line_no INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_price TEXT NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
"""

ITEMS = [
    ("FL-50", "Flange 50mm", "finished", "1000.00"),
    ("FL-80", "Flange 80mm", "finished", "1700.00"),
    ("RM-ST50", "Forged steel blank 50mm", "raw", "620.00"),
    ("RM-ST80", "Forged steel blank 80mm", "raw", "1050.00"),
]
BOM = [("FL-50", "RM-ST50", 1), ("FL-80", "RM-ST80", 1)]
LOCATIONS = [
    ("PLT-A", "Aurangabad Plant", "plant", "Aurangabad"),
    ("PLT-B", "Coimbatore Plant", "plant", "Coimbatore"),
    ("WH-BHW", "Bhiwandi Warehouse", "warehouse", "Bhiwandi"),
    ("WH-CHN", "Chennai Warehouse", "warehouse", "Chennai"),
    ("WH-GGN", "Gurgaon Warehouse", "warehouse", "Gurgaon"),
]

# location, item, on_hand, reserved, days since last count
STOCK = [
    ("WH-BHW", "FL-50", 220, 40, 0),
    ("WH-BHW", "FL-80", 60, 0, 0),
    ("WH-CHN", "FL-50", 90, 0, 9),
    ("WH-CHN", "FL-80", 70, 30, 9),
    ("WH-GGN", "FL-50", 35, 35, 1),
    ("WH-GGN", "FL-80", 25, 5, 1),
    ("PLT-A", "FL-50", 45, 0, 0),
    ("PLT-A", "RM-ST50", 500, 120, 0),
    ("PLT-A", "RM-ST80", 90, 10, 0),
    ("PLT-B", "RM-ST50", 160, 100, 0),
    ("PLT-B", "RM-ST80", 200, 20, 0),
]

# id, name, customer, valid_from offset, valid_to offset, {product: price}
PRICE_LISTS = [
    ("PL-STD-Q3", "Standard Q3", None, -122, -31, {"FL-50": "1180.00", "FL-80": "1980.00"}),
    ("PL-STD-Q4", "Standard Q4", None, -30, 60, {"FL-50": "1250.00", "FL-80": "2100.00"}),
    ("PL-STD-NEXT", "Standard next quarter", None, 61, 151, {"FL-50": "1290.00", "FL-80": "2160.00"}),
    ("PL-CUST-004", "Anand Engineering agreed list", "CUST-004", -60, 2,
     {"FL-50": "1215.00", "FL-80": "2040.00"}),
]


def seed(conn: sqlite3.Connection, today: date, now_iso: str) -> None:
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO items VALUES (?,?,?,?)", ITEMS)
    conn.executemany("INSERT INTO bom VALUES (?,?,?)", BOM)
    conn.executemany("INSERT INTO locations VALUES (?,?,?,?)", LOCATIONS)
    for location, item, on_hand, reserved, age_days in STOCK:
        counted = (today - timedelta(days=age_days)).isoformat()
        conn.execute(
            "INSERT INTO stock VALUES (?,?,?,?,?)", (location, item, on_hand, reserved, counted)
        )
    for list_id, name, customer, start, end, prices in PRICE_LISTS:
        conn.execute(
            "INSERT INTO price_lists VALUES (?,?,?,?,?,'INR')",
            (list_id, name, customer, (today + timedelta(start)).isoformat(),
             (today + timedelta(end)).isoformat()),
        )
        conn.executemany(
            "INSERT INTO price_list_items VALUES (?,?,?)",
            [(list_id, product, price) for product, price in prices.items()],
        )

    conn.executescript(SCHEMA_DOCS)
    _seed_rajhans_quote(conn, today)


def _seed_rajhans_quote(conn: sqlite3.Connection, today: date) -> None:
    """Q-6001: v1 was superseded by v2. The customer's late 'confirmed' refers to v1."""
    from limitara.hashing import content_hash

    valid_until = f"{(today + timedelta(days=6)).isoformat()}T12:30:00+00:00"  # Friday 18:00 IST
    promised = (today + timedelta(days=10)).isoformat()
    created = f"{(today - timedelta(days=3)).isoformat()}T05:30:00+00:00"
    conn.execute("INSERT INTO quotations VALUES ('Q-6001', 'CUST-006', 'CASE-SEED-6001', ?)", (created,))
    for version, price, status in ((1, "1112.50", "superseded"), (2, "1140.00", "sent")):
        line = {"product_id": "FL-50", "qty": 400, "unit_price": price, "promised_delivery_date": promised}
        digest = content_hash([line], [], valid_until, "seeded")
        conn.execute(
            "INSERT INTO quotation_versions VALUES ('Q-6001', ?, ?, ?, ?, 'seeded', ?, ?, ?, 'REP-01', ?)",
            (version, status, promised, valid_until, str(Decimal(price) * 400), digest,
             None if version == 1 else 1, created),
        )
        conn.execute(
            "INSERT INTO quotation_lines VALUES ('Q-6001', ?, 1, 'FL-50', 400, '1250.00', '11.00', ?, 'PL-STD-Q4', ?)",
            (version, price, promised),
        )

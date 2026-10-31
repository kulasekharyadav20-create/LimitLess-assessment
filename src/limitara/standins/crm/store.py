"""CRM stand-in data: accounts, contacts, staff, terms, opportunities, activity.

Seeded traps (asserted in tests/test_seed_crm.py):
- CUST-001 (the enquiring distributor): agreed 5% terms, prefers a single shipment,
  and has an open freight dispute (INV-8841). Only one of its contacts may confirm orders.
- CUST-002: agreed terms expired 10 days ago, and its credit is almost fully used.
- CUST-003: credit hold.
- CUST-004: margin floor override of 15%, and an agreed price list that is about to expire.
- CUST-005: new customer with no agreed terms at all.
- CUST-006: agreed 11% terms that, on their own, push the products below the 12% floor.
- Staff carry different margin authority: not everyone can approve everything.
"""

import sqlite3
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE staff(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('rep', 'sales_manager', 'sales_head', 'finance')),
    margin_authority_pct TEXT,
    credit_authority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE accounts(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    credit_status TEXT NOT NULL CHECK (credit_status IN ('ok', 'review', 'hold')),
    credit_limit TEXT NOT NULL,
    outstanding TEXT NOT NULL,
    owner_rep_id TEXT NOT NULL REFERENCES staff(id)
);
CREATE TABLE contacts(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    can_confirm_orders INTEGER NOT NULL
);
CREATE TABLE terms(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    discount_pct TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    partial_shipments TEXT NOT NULL CHECK (partial_shipments IN ('allowed', 'ask', 'not_allowed')),
    payment_days INTEGER NOT NULL,
    margin_floor_override_pct TEXT,
    note TEXT
);
CREATE TABLE opportunities(
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    title TEXT NOT NULL,
    stage TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE activities(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    at TEXT NOT NULL,
    by_staff TEXT REFERENCES staff(id)
);
"""

STAFF = [
    ("REP-01", "Meera Nair", "meera.nair@limitara.example", "rep", None, 0),
    ("REP-02", "Arjun Rao", "arjun.rao@limitara.example", "rep", None, 0),
    ("MGR-01", "Sanjay Kulkarni", "sanjay.kulkarni@limitara.example", "sales_manager", "9.00", 0),
    ("HEAD-01", "Divya Menon", "divya.menon@limitara.example", "sales_head", "5.00", 0),
    ("FIN-01", "Rohit Bansal", "rohit.bansal@limitara.example", "finance", None, 1),
]

ACCOUNTS = [
    ("CUST-001", "Shree Balaji Industrial Supplies", "Pune", "ok", "2500000.00", "600000.00", "REP-01"),
    ("CUST-002", "Konkan Pipes & Fittings", "Mumbai", "ok", "1500000.00", "1420000.00", "REP-02"),
    ("CUST-003", "Deccan Hydraulics", "Hyderabad", "hold", "1000000.00", "1180000.00", "REP-02"),
    ("CUST-004", "Anand Engineering Works", "Ahmedabad", "ok", "3000000.00", "400000.00", "REP-01"),
    ("CUST-005", "Metro Valve Co", "Delhi", "ok", "800000.00", "0.00", "REP-02"),
    ("CUST-006", "Rajhans Steel Traders", "Nagpur", "ok", "2000000.00", "350000.00", "REP-01"),
]

CONTACTS = [
    ("CT-101", "CUST-001", "Rakesh Shah", "rakesh.shah@balajisupplies.example", "Purchase manager", 1),
    ("CT-102", "CUST-001", "Priya Joshi", "priya.joshi@balajisupplies.example", "Accounts", 0),
    ("CT-103", "CUST-001", "Imran Sheikh", "imran.sheikh@balajisupplies.example", "Pune warehouse", 0),
    ("CT-201", "CUST-002", "Vikram Desai", "vikram@konkanpipes.example", "Owner", 1),
    ("CT-301", "CUST-003", "Lakshmi Reddy", "lakshmi@deccanhydraulics.example", "Purchase", 1),
    ("CT-401", "CUST-004", "Hitesh Patel", "hitesh@anandengg.example", "Purchase head", 1),
    ("CT-501", "CUST-005", "Neha Arora", "neha@metrovalve.example", "Procurement", 1),
    ("CT-601", "CUST-006", "Sunil Rajhans", "sunil@rajhanssteel.example", "Director", 1),
]

# id, account, discount, valid_from offset, valid_to offset, partials, payment days, floor override, note
TERMS = [
    ("TM-001", "CUST-001", "5.00", -200, 150, "ask", 45, None,
     "Prefers a single shipment. Freight dispute INV-8841 open."),
    ("TM-002", "CUST-002", "7.00", -200, -10, "allowed", 30, None, "Annual contract lapsed."),
    ("TM-003", "CUST-003", "4.00", -100, 120, "allowed", 30, None, None),
    ("TM-004", "CUST-004", "0.00", -60, 120, "allowed", 30, "15.00",
     "Agreed price list PL-CUST-004 instead of a discount. Floor 15%."),
    ("TM-006", "CUST-006", "11.00", -30, 90, "allowed", 60, None, "Large-volume contract."),
]

OPPORTUNITIES = [
    ("OP-01", "CUST-001", "Q4 flange supply, Pune warehouse", "negotiation", "595650.00"),
    ("OP-02", "CUST-002", "Renewal of annual contract", "proposal", "900000.00"),
    ("OP-04", "CUST-004", "Standing order FL-50", "won", "1200000.00"),
    ("OP-05", "CUST-005", "First trial order", "qualification", "250000.00"),
]


def seed(conn: sqlite3.Connection, today: date, now_iso: str) -> None:
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO staff VALUES (?,?,?,?,?,?)", STAFF)
    conn.executemany("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)", ACCOUNTS)
    conn.executemany("INSERT INTO contacts VALUES (?,?,?,?,?,?)", CONTACTS)
    for tid, account, disc, start, end, partials, days, floor, note in TERMS:
        conn.execute(
            "INSERT INTO terms VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, account, disc, (today + timedelta(start)).isoformat(),
             (today + timedelta(end)).isoformat(), partials, days, floor, note),
        )
    conn.executemany("INSERT INTO opportunities VALUES (?,?,?,?,?)", OPPORTUNITIES)

    def at(offset: int) -> str:
        return f"{(today + timedelta(offset)).isoformat()}T09:30:00+05:30"

    conn.executemany(
        "INSERT INTO activities(account_id, kind, summary, at, by_staff) VALUES (?,?,?,?,?)",
        [
            ("CUST-001", "order", "SO-Q3-0412 shipped in two parts (BHW 180, CHN 120)", at(-75), "REP-01"),
            ("CUST-001", "dispute_open",
             "INV-8841: customer disputes the second base freight charge on SO-Q3-0412. With Finance.",
             at(-6), "REP-01"),
            ("CUST-002", "call", "Asked for last year's 7% to continue; contract renewal pending", at(-4), "REP-02"),
            ("CUST-003", "note", "Credit hold placed by Finance: overdue invoices", at(-12), "FIN-01"),
        ],
    )

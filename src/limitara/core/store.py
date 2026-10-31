"""Core's own database: the process (what arrived, what we decided and why), not the stock or prices."""

import json
from pathlib import Path

from limitara.clock import to_wire
from limitara.db import Database, db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_cursor(id INTEGER PRIMARY KEY CHECK (id = 1), last_delivery_id INTEGER NOT NULL);
INSERT OR IGNORE INTO mail_cursor VALUES (1, 0);
CREATE TABLE IF NOT EXISTS inbound_messages(
    message_id TEXT PRIMARY KEY, first_delivery_id INTEGER, thread_id TEXT, customer_id TEXT,
    sent_at TEXT, received_at TEXT, status TEXT NOT NULL, intent TEXT, interpretation_json TEXT,
    content_hash TEXT, case_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_deliveries(
    delivery_id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, is_duplicate INTEGER NOT NULL, seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases(
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, thread_id TEXT, rep_id TEXT, status TEXT NOT NULL,
    destination_city TEXT, requested_by_date TEXT, quotation_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
    ref TEXT, details_json TEXT
);
CREATE TABLE IF NOT EXISTS plan_bases(
    quotation_id TEXT NOT NULL, version INTEGER NOT NULL, plan_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
    request_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (quotation_id, version)
);
CREATE TABLE IF NOT EXISTS price_bases(
    quotation_id TEXT NOT NULL, version INTEGER NOT NULL, line_no INTEGER NOT NULL, basis_json TEXT NOT NULL,
    PRIMARY KEY (quotation_id, version, line_no)
);
CREATE TABLE IF NOT EXISTS flags(
    id INTEGER PRIMARY KEY AUTOINCREMENT, quotation_id TEXT NOT NULL, version INTEGER NOT NULL, code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('block', 'needs_role', 'info')), required_role TEXT, detail TEXT NOT NULL,
    provenance_json TEXT, resolved_by TEXT, resolved_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, quotation_id TEXT NOT NULL, version INTEGER NOT NULL, required_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'changed')),
    decided_by TEXT, decided_at TEXT, reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbound_messages(
    id TEXT PRIMARY KEY, case_id TEXT, kind TEXT NOT NULL, audience TEXT NOT NULL, to_addr TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, content_hash TEXT, refs_json TEXT,
    state TEXT NOT NULL CHECK (state IN ('draft', 'awaiting_approval', 'approved', 'sent', 'cancelled')),
    approved_basis TEXT, approved_by TEXT, sent_at TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders_meta(
    order_id TEXT PRIMARY KEY, quotation_id TEXT NOT NULL, version INTEGER NOT NULL, case_id TEXT, customer_id TEXT,
    plan_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE (quotation_id, version)
);
CREATE TABLE IF NOT EXISTS intents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done', 'stuck')), attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, next_try_at TEXT, result_json TEXT, group_ref TEXT, seq INTEGER
);
CREATE TABLE IF NOT EXISTS slip_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, work_order_id TEXT, promised TEXT, forecast TEXT,
    detected_at TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, options_json TEXT, decision_json TEXT
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT, text TEXT NOT NULL,
    created_at TEXT NOT NULL, read_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open', refs_json TEXT, created_at TEXT NOT NULL, resolved_by TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS consents(
    case_id TEXT NOT NULL, quotation_id TEXT NOT NULL, version INTEGER NOT NULL, recorded_by TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (quotation_id, version)
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL,
    entity TEXT, entity_id TEXT, details_json TEXT
);
"""


class CoreStore:
    def __init__(self, path: str | Path | None = None, clock=None):
        self.db = Database(path or db_path("core"))
        self.clock = clock
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)

    def now(self) -> str:
        return to_wire(self.clock.now())

    def audit(self, actor_type: str, actor_id: str | None, action: str, entity: str = "", entity_id: str = "", **details) -> None:
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO audit_log(at, actor_type, actor_id, action, entity, entity_id, details_json) VALUES (?,?,?,?,?,?,?)",
                (self.now(), actor_type, actor_id, action, entity, entity_id, json.dumps(details, default=str)),
            )

    def event(self, case_id: str, kind: str, ref: str = "", **details) -> None:
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO case_events(case_id, at, kind, ref, details_json) VALUES (?,?,?,?,?)",
                (case_id, self.now(), kind, ref, json.dumps(details, default=str)),
            )

    def task(self, case_id: str | None, role: str, kind: str, text: str, **refs) -> int:
        with self.db.lock:
            cur = self.db.conn.execute(
                "INSERT INTO tasks(case_id, role, kind, text, refs_json, created_at) VALUES (?,?,?,?,?,?)",
                (case_id, role, kind, text, json.dumps(refs, default=str), self.now()),
            )
        self.audit("system", None, "task_created", "task", str(cur.lastrowid), kind=kind, role=role, text=text)
        return cur.lastrowid

    def alert(self, user_id: str, kind: str, text: str, ref: str = "") -> None:
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO alerts(user_id, kind, ref, text, created_at) VALUES (?,?,?,?,?)",
                (user_id, kind, ref, text, self.now()),
            )

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from limitara.contracts import Unavailable, Unknown
from limitara.core.clients import StandIn
from limitara.core.snapshot import SnapshotBuilder


def test_a_404_becomes_unknown_never_zero():
    app = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"error": "nope"})), base_url="http://x")
    with pytest.raises(Unknown) as err:
        StandIn("production", app).get("/capacity")
    assert "no record" in str(err.value)


def test_a_503_and_a_timeout_become_unavailable_unknown():
    down = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503, json={})), base_url="http://x")
    with pytest.raises(Unavailable):
        StandIn("erp", down).get("/stock")

    def boom(request):
        raise httpx.ConnectTimeout("slow")

    slow = httpx.Client(transport=httpx.MockTransport(boom), base_url="http://x")
    with pytest.raises(Unavailable):
        StandIn("erp", slow).get("/stock")


def test_reads_come_back_stamped(clients):
    stamped = clients.erp.get("/items")
    assert stamped.source == "erp" and stamped.read_at.tzinfo is not None


def test_snapshot_has_every_part_and_stamps_each_source(clients, clock):
    snap = SnapshotBuilder(clients, clock).build()
    assert snap.today == date(2026, 10, 31)
    assert snap.stock[("WH-BHW", "FL-50")].available == 180
    assert set(snap.reads) == {"erp", "production", "dispatch"}
    assert ("A1", date(2026, 11, 3)) in snap.capacity and snap.capacity[("A1", date(2026, 11, 3))].free == 0
    assert snap.schedules[("WH-BHW", "Pune")].transit == 1
    assert snap.bom["FL-50"] == ("RM-ST50", 1)


def test_snapshot_stops_when_a_stand_in_is_down(clients, clock):
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})), base_url="http://x")
    clients.production.http = broken
    with pytest.raises(Unknown):
        SnapshotBuilder(clients, clock).build()


def test_stale_read_is_re_read_once_then_refused(clients, clock):
    calls = {"n": 0}
    real = clients.erp.get

    def stale_then_fresh(path, **params):
        s = real(path, **params)
        if path == "/stock":
            calls["n"] += 1
            if calls["n"] == 1:
                from dataclasses import replace
                return replace(s, read_at=s.read_at - timedelta(hours=1))
        return s

    clients.erp.get = stale_then_fresh
    SnapshotBuilder(clients, clock).build()
    assert calls["n"] == 2  # re-read once, then fine

    def always_stale(path, **params):
        from dataclasses import replace
        s = real(path, **params)
        return replace(s, read_at=s.read_at - timedelta(hours=1)) if path == "/stock" else s

    clients.erp.get = always_stale
    with pytest.raises(Unknown) as err:
        SnapshotBuilder(clients, clock).build()
    assert "stock" in str(err.value)


def test_owner_holds_are_added_back_so_a_quote_can_verify_itself(clients, clock):
    body = {"ref_id": "Q-1:v1", "kind": "hard", "items": [{"location_id": "WH-BHW", "item_id": "FL-50", "qty": 100}]}
    clients.erp.post("/reservations", json=body, key="K1")
    plain = SnapshotBuilder(clients, clock).build()
    credited = SnapshotBuilder(clients, clock).build(owner_refs=("Q-1:v1",))
    assert plain.stock[("WH-BHW", "FL-50")].available == 80
    assert credited.stock[("WH-BHW", "FL-50")].available == 180


def test_store_creates_every_table_and_writes_audit(store):
    names = {r["name"] for r in store.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"inbound_messages", "cases", "flags", "approvals", "outbound_messages", "orders_meta", "intents",
            "slip_events", "alerts", "tasks", "audit_log", "plan_bases"} <= names
    store.audit("system", None, "test", "x", "1", a=1)
    assert store.db.one("SELECT COUNT(*) AS c FROM audit_log")["c"] == 1

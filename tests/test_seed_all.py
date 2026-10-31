from datetime import UTC, date, datetime

from limitara.db import connect
from limitara.seed import STAND_INS, seed_all

NOW = datetime(2026, 10, 31, 5, 30, tzinfo=UTC)


def test_seed_builds_one_database_per_stand_in(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    paths = seed_all(date(2026, 10, 31), NOW)
    assert set(paths) == set(STAND_INS)
    assert len(set(paths.values())) == 5


def test_reseeding_wipes_changes_made_during_a_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    paths = seed_all(date(2026, 10, 31), NOW)
    conn = connect(paths["erp"])
    conn.execute("UPDATE stock SET reserved = 0")
    conn.close()
    paths = seed_all(date(2026, 10, 31), NOW)
    conn = connect(paths["erp"])
    assert conn.execute("SELECT SUM(reserved) FROM stock").fetchone()[0] > 0


def test_customer_ids_agree_across_stand_ins(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    paths = seed_all(date(2026, 10, 31), NOW)
    known = {r[0] for r in connect(paths["crm"]).execute("SELECT id FROM accounts")}
    for db, sql in (
        ("mailbox", "SELECT DISTINCT customer_id FROM threads"),
        ("dispatch", "SELECT DISTINCT customer_id FROM shipments"),
        ("production", "SELECT DISTINCT customer_id FROM work_orders"),
        ("erp", "SELECT DISTINCT customer_id FROM price_lists WHERE customer_id IS NOT NULL"),
    ):
        assert {r[0] for r in connect(paths[db]).execute(sql)} <= known, db


def test_product_ids_agree_across_stand_ins(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    paths = seed_all(date(2026, 10, 31), NOW)
    known = {r[0] for r in connect(paths["erp"]).execute("SELECT id FROM items WHERE kind='finished'")}
    assert {r[0] for r in connect(paths["production"]).execute("SELECT product_id FROM line_products")} <= known
    assert {r[0] for r in connect(paths["dispatch"]).execute("SELECT product_id FROM shipment_lines")} <= known

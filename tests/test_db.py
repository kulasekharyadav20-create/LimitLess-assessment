import pytest

from limitara.db import connect, db_path, reset


def test_each_stand_in_gets_its_own_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    assert db_path("erp") != db_path("crm")
    assert db_path("erp").parent == tmp_path


def test_reset_removes_database_and_wal_files(tmp_path):
    path = tmp_path / "x.db"
    conn = connect(path)
    conn.execute("CREATE TABLE t(a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    reset(path)
    assert list(tmp_path.iterdir()) == []


def test_reset_on_missing_file_is_fine(tmp_path):
    reset(tmp_path / "nothing.db")


def test_foreign_keys_are_enforced(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE a(id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE b(a_id TEXT REFERENCES a(id))")
    with pytest.raises(Exception):
        conn.execute("INSERT INTO b VALUES ('missing')")

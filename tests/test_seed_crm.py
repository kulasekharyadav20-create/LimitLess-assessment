from datetime import date
from decimal import Decimal as D

import pytest

from limitara.db import connect
from limitara.money import apply_discount, meets_floor
from limitara.standins.crm.store import seed

TODAY = date(2026, 10, 31)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "crm.db")
    seed(c, TODAY, "2026-10-31T05:30:00+00:00")
    return c


def terms_for(conn, account):
    return conn.execute("SELECT * FROM terms WHERE account_id=?", (account,)).fetchone()


def test_enquiring_customer_has_valid_terms_and_prefers_one_shipment(conn):
    t = terms_for(conn, "CUST-001")
    assert t["valid_from"] <= TODAY.isoformat() <= t["valid_to"]
    assert t["partial_shipments"] == "ask"


def test_enquiring_customer_has_an_open_freight_dispute(conn):
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM activities WHERE account_id='CUST-001'")]
    assert "dispute_open" in kinds and "dispute_closed" not in kinds


def test_only_one_contact_of_the_enquirer_may_confirm_orders(conn):
    rows = conn.execute("SELECT name FROM contacts WHERE account_id='CUST-001' AND can_confirm_orders=1").fetchall()
    assert [r["name"] for r in rows] == ["Rakesh Shah"]


def test_one_customers_terms_have_expired(conn):
    assert terms_for(conn, "CUST-002")["valid_to"] < TODAY.isoformat()


def test_one_customer_is_on_credit_hold(conn):
    assert conn.execute("SELECT credit_status FROM accounts WHERE id='CUST-003'").fetchone()[0] == "hold"


def test_one_customer_has_almost_no_credit_headroom(conn):
    a = conn.execute("SELECT credit_limit, outstanding FROM accounts WHERE id='CUST-002'").fetchone()
    assert D(a["credit_limit"]) - D(a["outstanding"]) == D("80000.00")


def test_new_customer_has_no_terms_at_all(conn):
    assert terms_for(conn, "CUST-005") is None


def test_customer_has_a_floor_override(conn):
    assert terms_for(conn, "CUST-004")["margin_floor_override_pct"] == "15.00"


def test_cust006_agreed_discount_alone_breaks_the_company_floor(conn):
    disc = D(terms_for(conn, "CUST-006")["discount_pct"])
    for list_price, cost in ((D("1250.00"), D("1000.00")), (D("2100.00"), D("1700.00"))):
        assert not meets_floor(apply_discount(list_price, disc), cost, D("12"))


def test_staff_have_different_margin_authority(conn):
    levels = {r["id"]: r["margin_authority_pct"] for r in conn.execute("SELECT id, margin_authority_pct FROM staff")}
    assert levels["MGR-01"] == "9.00" and levels["HEAD-01"] == "5.00" and levels["REP-01"] is None

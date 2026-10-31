from datetime import date

import pytest

from limitara.db import connect
from limitara.standins.mailbox.store import seed

TODAY = date(2026, 10, 31)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "mailbox.db")
    seed(c, TODAY, "2026-10-31T05:30:00+00:00")
    return c


def test_freight_dispute_thread_ends_with_an_unanswered_customer_message(conn):
    last = conn.execute(
        "SELECT from_addr FROM deliveries WHERE thread_id='T-1002' ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    assert last["from_addr"].endswith("@balajisupplies.example")


def test_a_message_tells_us_to_skip_approval(conn):
    body = conn.execute("SELECT body FROM deliveries WHERE message_id='<k1@konkan>'").fetchone()["body"]
    assert "skip approval" in body


def test_same_message_id_was_delivered_twice(conn):
    rows = conn.execute("SELECT delivery_id FROM deliveries WHERE message_id='<p1@anandengg>'").fetchall()
    assert len(rows) == 2


def test_purchase_order_attachment_disagrees_with_the_body(conn):
    row = conn.execute(
        "SELECT d.body, a.content FROM deliveries d JOIN attachments a USING(delivery_id) "
        "WHERE d.message_id='<p1@anandengg>' LIMIT 1"
    ).fetchone()
    assert "150 x" in row["body"] and "Qty: 1500" in row["content"]


def test_confirmation_quotes_v1_and_arrived_after_v2_existed(conn):
    ok = conn.execute("SELECT * FROM deliveries WHERE message_id='<ok1@rajhans>'").fetchone()
    v2 = conn.execute("SELECT sent_at FROM deliveries WHERE message_id='<v2@limitara>'").fetchone()
    assert "v1" in ok["body"] and ok["in_reply_to"] == "<v1@limitara>"
    assert ok["sent_at"] < v2["sent_at"] < ok["received_at"]


def test_every_thread_belongs_to_a_known_customer_format(conn):
    ids = {r["customer_id"] for r in conn.execute("SELECT customer_id FROM threads")}
    assert ids == {"CUST-001", "CUST-002", "CUST-003", "CUST-004", "CUST-006"}


def test_timestamps_carry_the_india_offset(conn):
    sent = conn.execute("SELECT sent_at FROM deliveries LIMIT 1").fetchone()["sent_at"]
    assert sent.endswith("+05:30")

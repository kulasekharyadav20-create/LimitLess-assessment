def test_crm_finds_an_account_and_contact_by_sender_address(crm):
    row = crm.get("/accounts/by-email", params={"email": "Rakesh.Shah@balajisupplies.example"}).json()["data"]
    assert row["id"] == "CUST-001" and row["can_confirm_orders"] == 1


def test_crm_unknown_sender_is_404(crm):
    assert crm.get("/accounts/by-email", params={"email": "stranger@nowhere.example"}).status_code == 404


def test_crm_returns_every_terms_row_with_validity_and_does_not_choose(crm):
    rows = crm.get("/accounts/CUST-002/terms").json()["data"]
    assert len(rows) == 1 and rows[0]["valid_to"] < "2026-10-31"


def test_crm_staff_carry_margin_authority(crm):
    staff = {s["id"]: s["margin_authority_pct"] for s in crm.get("/staff").json()["data"]}
    assert staff["MGR-01"] == "9.00" and staff["HEAD-01"] == "5.00"


def test_crm_activity_logging_is_idempotent(crm):
    body = {"kind": "note", "summary": "quote sent", "by_staff": "REP-01"}
    a = crm.post("/accounts/CUST-001/activities", json=body, headers={"Idempotency-Key": "L1"}).json()
    b = crm.post("/accounts/CUST-001/activities", json=body, headers={"Idempotency-Key": "L1"}).json()
    assert a == b
    kinds = [x["kind"] for x in crm.get("/accounts/CUST-001/activities").json()["data"]]
    assert kinds.count("note") == 1


def test_crm_unknown_account_is_404(crm):
    assert crm.get("/accounts/CUST-999").status_code == 404


def test_mailbox_lists_deliveries_after_a_cursor(mailbox):
    first = mailbox.get("/deliveries", params={"limit": 3}).json()["data"]
    assert len(first) == 3
    rest = mailbox.get("/deliveries", params={"after_id": first[-1]["delivery_id"]}).json()["data"]
    assert all(d["delivery_id"] > first[-1]["delivery_id"] for d in rest)


def test_mailbox_marks_our_own_mail_as_outgoing(mailbox):
    deliveries = mailbox.get("/deliveries", params={"limit": 200}).json()["data"]
    ours = [d for d in deliveries if d["from_addr"].endswith("@limitara.example")]
    assert ours and all(d["direction"] == "out" for d in ours)


def test_mailbox_inbound_twice_creates_two_deliveries(mailbox):
    body = {"message_id": "<dup@x>", "thread_id": "T-9", "customer_id": "CUST-001", "from_addr": "a@x.example", "body": "hi"}
    mailbox.post("/inbound", json=body)
    mailbox.post("/inbound", json=body)
    rows = [d for d in mailbox.get("/deliveries", params={"limit": 500}).json()["data"] if d["message_id"] == "<dup@x>"]
    assert len(rows) == 2


def test_mailbox_outbound_with_the_same_key_sends_once(mailbox):
    body = {"message_id": "<o1@limitara>", "to_addr": "c@x.example", "subject": "Quote", "body": "hello"}
    mailbox.post("/outbound", json=body, headers={"Idempotency-Key": "OUT-1"})
    mailbox.post("/outbound", json=body, headers={"Idempotency-Key": "OUT-1"})
    rows = [d for d in mailbox.get("/deliveries", params={"limit": 500}).json()["data"] if d["message_id"] == "<o1@limitara>"]
    assert len(rows) == 1 and rows[0]["direction"] == "out"


def test_mailbox_thread_returns_history_with_attachments(mailbox):
    t = mailbox.get("/threads/T-4001").json()["data"]
    assert len(t["deliveries"]) == 2 and t["deliveries"][0]["attachments"][0]["filename"] == "PO-7781.txt"

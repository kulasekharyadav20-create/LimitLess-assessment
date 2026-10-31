"""Cases: one customer need, from enquiry to delivery."""

from limitara.core.context import Ctx


def open_case(ctx: Ctx, customer_id: str, thread_id: str | None, rep_id: str | None, contact_email: str | None = None,
              last_message_id: str | None = None, city: str | None = None, deliver_by: str | None = None) -> str:
    db = ctx.store.db
    with db.tx() as conn:
        n = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
        cid = f"CASE-{n:04d}"
        now = ctx.store.now()
        conn.execute(
            "INSERT INTO cases(id, customer_id, thread_id, rep_id, status, destination_city, requested_by_date, contact_email,"
            " last_message_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cid, customer_id, thread_id, rep_id, "open", city, deliver_by, contact_email, last_message_id, now, now))
    ctx.store.event(cid, "case_opened", customer_id=customer_id)
    ctx.store.audit("system", None, "case_opened", "case", cid, customer_id=customer_id)
    return cid


def get_case(ctx: Ctx, case_id: str) -> dict | None:
    return ctx.store.db.one("SELECT * FROM cases WHERE id=?", (case_id,))


def set_status(ctx: Ctx, case_id: str, status: str) -> None:
    with ctx.store.db.tx() as conn:
        conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?", (status, ctx.store.now(), case_id))

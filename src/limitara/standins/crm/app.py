"""CRM stand-in: accounts, contacts, staff, terms, activity. Returns records, never decisions."""

from fastapi import FastAPI, Header
from pydantic import BaseModel

from limitara.clock import Clock, clock_from_env, to_wire
from limitara.db import Database, db_path
from limitara.standins.base import ApiError, create_base_app, idempotent, stamped


class ActivityIn(BaseModel):
    kind: str
    summary: str
    by_staff: str | None = None


def create_app(path: str | None = None, clock: Clock | None = None) -> FastAPI:
    db = Database(path or db_path("crm"))
    app = create_base_app("crm", db, clock or clock_from_env())

    def account_or_404(account_id: str) -> dict:
        row = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if row is None:
            raise ApiError(404, "no_such_account")
        return row

    @app.get("/accounts")
    def accounts():
        return stamped(app, db.query("SELECT * FROM accounts ORDER BY id"))

    @app.get("/accounts/by-email")
    def by_email(email: str):
        row = db.one(
            "SELECT a.*, c.id AS contact_id, c.name AS contact_name, c.can_confirm_orders FROM contacts c "
            "JOIN accounts a ON a.id = c.account_id WHERE lower(c.email)=lower(?)", (email,)
        )
        if row is None:
            raise ApiError(404, "unknown_sender", email)
        return stamped(app, row)

    @app.get("/accounts/{account_id}")
    def account(account_id: str):
        return stamped(app, account_or_404(account_id))

    @app.get("/accounts/{account_id}/contacts")
    def contacts(account_id: str):
        account_or_404(account_id)
        return stamped(app, db.query("SELECT * FROM contacts WHERE account_id=? ORDER BY id", (account_id,)))

    @app.get("/accounts/{account_id}/terms")
    def terms(account_id: str):
        account_or_404(account_id)
        return stamped(app, db.query("SELECT * FROM terms WHERE account_id=? ORDER BY valid_from", (account_id,)))

    @app.get("/accounts/{account_id}/activities")
    def activities(account_id: str):
        account_or_404(account_id)
        return stamped(app, db.query("SELECT * FROM activities WHERE account_id=? ORDER BY id", (account_id,)))

    @app.post("/accounts/{account_id}/activities")
    def log_activity(account_id: str, body: ActivityIn, idempotency_key: str | None = Header(None)):
        account_or_404(account_id)

        def work(conn, now):
            cur = conn.execute(
                "INSERT INTO activities(account_id, kind, summary, at, by_staff) VALUES (?,?,?,?,?)",
                (account_id, body.kind, body.summary, now, body.by_staff),
            )
            return dict(conn.execute("SELECT * FROM activities WHERE id=?", (cur.lastrowid,)).fetchone())

        return idempotent(app, idempotency_key, {"account": account_id, **body.model_dump()}, work)

    @app.get("/staff")
    def staff():
        return stamped(app, db.query("SELECT * FROM staff ORDER BY id"))

    @app.get("/staff/{staff_id}")
    def one_staff(staff_id: str):
        row = db.one("SELECT * FROM staff WHERE id=?", (staff_id,))
        if row is None:
            raise ApiError(404, "no_such_staff")
        return stamped(app, row)

    @app.post("/admin/accounts/{account_id}/credit")
    def set_credit(account_id: str, credit_status: str):
        account_or_404(account_id)
        with db.tx() as conn:
            conn.execute("UPDATE accounts SET credit_status=? WHERE id=?", (credit_status, account_id))
        return {"ok": True}

    return app

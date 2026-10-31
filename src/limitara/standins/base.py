"""Shared plumbing for the five stand-ins: app factory, read envelope, safe-repeat writes."""

import hashlib
import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from limitara.clock import Clock, to_wire
from limitara.db import Database

IDEMPOTENCY_DDL = """
CREATE TABLE IF NOT EXISTS idempotency_keys(
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


def create_base_app(name: str, db: Database, clock: Clock) -> FastAPI:
    app = FastAPI(title=f"{name} stand-in")
    app.state.name, app.state.db, app.state.clock = name, db, clock
    with db.lock:
        db.conn.executescript(IDEMPOTENCY_DDL)

    @app.exception_handler(ApiError)
    async def _api_error(_request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})

    @app.get("/health")
    def health():
        return {"ok": True, "name": name}

    return app


def now_wire(app: FastAPI) -> str:
    return to_wire(app.state.clock.now())


def stamped(app: FastAPI, data) -> dict:
    return {"source": app.state.name, "read_at": now_wire(app), "data": data}


def idempotent(app: FastAPI, key: str | None, request, work: Callable) -> dict:
    """Run work(conn, now) once per key. Same key and request replays; a different request conflicts."""
    if not key:
        raise ApiError(400, "missing_idempotency_key", "send an Idempotency-Key header")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
    now = now_wire(app)
    with app.state.db.tx() as conn:
        row = conn.execute(
            "SELECT request_hash, response_json FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
        if row:
            if row["request_hash"] != digest:
                raise ApiError(409, "idempotency_conflict", f"key {key} was used with a different request")
            return json.loads(row["response_json"])
        result = work(conn, now)
        conn.execute(
            "INSERT INTO idempotency_keys VALUES (?,?,?,?)", (key, digest, json.dumps(result, default=str), now)
        )
        return result

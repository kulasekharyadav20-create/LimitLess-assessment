"""One wrapper per stand-in. Failures become Unknown, never zero; reads come back Stamped."""

from dataclasses import dataclass

import httpx

from limitara.clock import parse_instant
from limitara.contracts import Conflict, Rejected, Stamped, Unavailable, Unknown
from limitara.core.config import DEFAULT, Config
from limitara.ports import STANDINS, url_of


class StandIn:
    def __init__(self, name: str, http: httpx.Client):
        self.name, self.http = name, http

    def get(self, path: str, **params) -> Stamped:
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            r = self.http.get(path, params=clean)
        except httpx.HTTPError as exc:
            raise Unavailable(self.name, f"unreachable: {exc.__class__.__name__}") from exc
        if r.status_code == 404:
            raise Unknown(self.name, f"no record: {r.json().get('error', 'not found')}")
        if r.status_code >= 500:
            raise Unavailable(self.name, f"server error {r.status_code}")
        if r.status_code >= 400:
            raise Rejected(self.name, r.json().get("error", str(r.status_code)), r.json().get("detail", ""))
        body = r.json()
        return Stamped(body["source"], parse_instant(body["read_at"]), body["data"])

    def post(self, path: str, json=None, key: str | None = None, method: str = "POST") -> dict:
        headers = {"Idempotency-Key": key} if key else {}
        try:
            r = self.http.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise Unavailable(self.name, f"unreachable: {exc.__class__.__name__}") from exc
        if r.status_code >= 500:
            raise Unavailable(self.name, f"server error {r.status_code}")
        body = r.json()
        if r.status_code == 409:
            raise Conflict(f"{self.name}: {body}")
        if r.status_code == 404:
            raise Unknown(self.name, f"no record: {body.get('error')}")
        if r.status_code >= 400:
            raise Rejected(self.name, body.get("error", str(r.status_code)), body.get("detail", ""))
        return body


@dataclass
class Clients:
    mailbox: StandIn
    crm: StandIn
    erp: StandIn
    production: StandIn
    dispatch: StandIn

    @classmethod
    def from_env(cls, config: Config = DEFAULT) -> "Clients":
        def make(name: str) -> StandIn:
            timeout = httpx.Timeout(config.write_timeout, read=config.read_timeout)
            return StandIn(name, httpx.Client(base_url=url_of(name), timeout=timeout))

        return cls(**{name: make(name) for name in STANDINS})

    @classmethod
    def from_http(cls, **http_clients: httpx.Client) -> "Clients":
        return cls(**{name: StandIn(name, http_clients[name]) for name in STANDINS})

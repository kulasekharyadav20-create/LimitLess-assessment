"""Where each program listens. Override with env vars (tests use a private port range)."""

import os

STANDINS = ("mailbox", "crm", "erp", "production", "dispatch")


def base_port() -> int:
    return int(os.environ.get("LIMITARA_PORT_BASE", "4001"))


def port_of(name: str) -> int:
    return base_port() + STANDINS.index(name)


def core_port() -> int:
    return int(os.environ.get("LIMITARA_CORE_PORT", "3000"))


def url_of(name: str) -> str:
    return os.environ.get(f"LIMITARA_{name.upper()}_URL") or f"http://127.0.0.1:{port_of(name)}"

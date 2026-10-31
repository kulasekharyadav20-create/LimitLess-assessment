"""The architectural promises, checked by reading the code itself."""

import dataclasses
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "limitara"

PURE = [
    "money.py", "calendar.py", "hashing.py", "quote_states.py", "freshness.py",
    "core/promise/model.py", "core/promise/search.py", "core/promise/select.py", "core/promise/verify.py",
    "core/promise/explain.py", "core/promise/serialize.py", "core/pricing/engine.py", "core/pricing/flags.py",
    "core/approvals/router.py", "core/intake/interpreter.py", "core/orders/stage.py", "core/whatif.py",
]
FORBIDDEN_IMPORTS = ("httpx", "sqlite3", "requests", "fastapi", "limitara.db", "limitara.core.clients", "limitara.core.store",
                     "limitara.core.context")
FORBIDDEN_CALLS = ("datetime.now(", "date.today(", "time.time(", "random.")


def source(rel: str) -> str:
    return (SRC / rel).read_text()


def test_the_decision_logic_does_no_io_and_never_reads_the_clock_itself():
    for rel in PURE:
        text = source(rel)
        imports = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", text, flags=re.M)
        for bad in FORBIDDEN_IMPORTS:
            assert not any(i == bad or i.startswith(bad + ".") for i in imports), f"{rel} imports {bad}"
        for names in re.findall(r"from limitara\.clock import ([^\n]+)", text):
            assert not re.search(r"SystemClock|clock_from_env|FixedClock|\bClock\b", names), f"{rel} uses a live clock"
        for call in FORBIDDEN_CALLS:
            assert call not in text, f"{rel} calls {call}"


def test_only_the_outbound_gate_posts_mail_to_customers():
    offenders = [str(p.relative_to(SRC)) for p in (SRC / "core").rglob("*.py")
                 if '"/outbound"' in p.read_text() and p.name != "gate.py"]
    assert offenders == []


def test_no_input_to_the_pricing_function_can_come_from_email_text():
    from limitara.core.pricing.engine import PricingInputs

    fields = {f.name for f in dataclasses.fields(PricingInputs)}
    assert fields == {"customer_id", "today", "lists", "terms", "cost", "lines", "asked_valid_until", "rep_discount_pct", "reads"}
    # the only discount input is rep_discount_pct, set by revise() from a signed-in user, never by intake
    intake_src = source("core/intake/service.py") + source("core/intake/interpreter.py")
    assert "rep_discount_pct" not in intake_src


def test_the_audit_log_is_append_only_in_our_code():
    for p in SRC.rglob("*.py"):
        text = p.read_text()
        assert not re.search(r"(UPDATE|DELETE\s+FROM)\s+audit_log", text, flags=re.I), p


def test_only_core_clients_talk_http_to_the_stand_ins():
    offenders = [str(p.relative_to(SRC)) for p in (SRC / "core").rglob("*.py")
                 if "httpx" in p.read_text() and p.name not in ("clients.py",)]
    assert offenders == []


def test_every_stand_in_write_endpoint_demands_an_idempotency_key():
    for name in ("erp", "production", "dispatch", "crm", "mailbox"):
        text = source(f"standins/{name}/app.py")
        posts = re.findall(r'@app\.post\("([^"]+)"\)', text)
        gated = [p for p in posts if not p.startswith("/admin") and p != "/inbound"]
        assert gated, name
        assert text.count("idempotent(") >= len(gated), f"{name}: some write skips the idempotency check"

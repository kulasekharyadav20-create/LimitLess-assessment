from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from limitara.clock import FixedClock
from limitara.seed import seed_all

NOW = datetime(2026, 10, 31, 5, 30, tzinfo=UTC)  # Saturday 11:00 IST
TODAY = date(2026, 10, 31)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMITARA_DATA_DIR", str(tmp_path))
    seed_all(TODAY, NOW)
    return tmp_path


def _client(factory, data_dir, name, clock):
    return TestClient(factory(str(data_dir / f"{name}.db"), clock))


@pytest.fixture
def erp(data_dir, clock):
    from limitara.standins.erp.app import create_app

    return _client(create_app, data_dir, "erp", clock)


@pytest.fixture
def production(data_dir, clock):
    from limitara.standins.production.app import create_app

    return _client(create_app, data_dir, "production", clock)


@pytest.fixture
def dispatch(data_dir, clock):
    from limitara.standins.dispatch.app import create_app

    return _client(create_app, data_dir, "dispatch", clock)


@pytest.fixture
def crm(data_dir, clock):
    from limitara.standins.crm.app import create_app

    return _client(create_app, data_dir, "crm", clock)


@pytest.fixture
def mailbox(data_dir, clock):
    from limitara.standins.mailbox.app import create_app

    return _client(create_app, data_dir, "mailbox", clock)

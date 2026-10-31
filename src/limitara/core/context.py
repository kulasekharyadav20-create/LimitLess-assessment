"""What every Core service needs, passed explicitly so tests can swap any part."""

from dataclasses import dataclass

from limitara.clock import Clock
from limitara.core.clients import Clients
from limitara.core.config import Config
from limitara.core.snapshot import SnapshotBuilder
from limitara.core.store import CoreStore


@dataclass
class Ctx:
    clients: Clients
    store: CoreStore
    clock: Clock
    config: Config

    @property
    def builder(self) -> SnapshotBuilder:
        return SnapshotBuilder(self.clients, self.clock, self.config)

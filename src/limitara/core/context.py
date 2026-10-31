"""What every Core service needs, passed explicitly so tests can swap any part."""

import threading
from dataclasses import dataclass, field

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
    lock: threading.RLock = field(default_factory=threading.RLock)  # one worker at a time on shared cases

    @property
    def builder(self) -> SnapshotBuilder:
        return SnapshotBuilder(self.clients, self.clock, self.config)

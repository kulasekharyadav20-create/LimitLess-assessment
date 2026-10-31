"""Plain data the promise engine works on. No I/O anywhere in this folder."""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal


@dataclass(frozen=True)
class StockRow:
    on_hand: int
    reserved: int
    counted_at: str

    @property
    def available(self) -> int:
        return max(0, self.on_hand - self.reserved)


@dataclass(frozen=True)
class CapRow:
    total: int
    booked: int
    status: str
    note: str | None = None

    @property
    def free(self) -> int:
        return max(0, self.total - self.booked) if self.status == "open" else 0


@dataclass(frozen=True)
class Schedule:
    weekdays: frozenset[int]
    cutoff: time
    transit: int
    base: Decimal
    per_unit: Decimal
    carrier: str


@dataclass(frozen=True)
class Lane:
    transit: int
    per_unit: Decimal


@dataclass(frozen=True)
class Snapshot:
    now: datetime                                   # UTC instant the snapshot was taken
    today: date                                     # business date in IST
    now_ist_time: time
    cost: dict[str, Decimal]                        # finished item -> unit cost
    bom: dict[str, tuple[str, int]]                 # product -> (material, qty per unit)
    stock: dict[tuple[str, str], StockRow]          # (location, item) -> row
    capacity: dict[tuple[str, date], CapRow]        # (line, day) -> row
    line_plant: dict[str, str]
    line_products: dict[str, frozenset[str]]
    lanes: dict[tuple[str, str], Lane]              # (plant, warehouse) -> lane
    schedules: dict[tuple[str, str], Schedule]      # (warehouse, city) -> schedule
    holidays: frozenset[date]
    warehouses: frozenset[str]
    plants: frozenset[str]
    reads: dict[str, datetime] = field(default_factory=dict)  # source -> read_at


@dataclass(frozen=True)
class Request:
    lines: tuple[tuple[str, int], ...]              # (product, qty)
    city: str
    deliver_by: date
    partial: str = "allowed"                        # allowed | ask | not_allowed


@dataclass(frozen=True)
class Alloc:
    kind: str                                       # stock | plant_stock | production
    product: str
    qty: int
    location: str                                   # warehouse (stock) or plant (others)
    avail_from: date                                # earliest date it can leave the shipping warehouse
    line: str | None = None
    day: date | None = None


@dataclass(frozen=True)
class Shipment:
    warehouse: str
    ship_date: date
    delivery: date
    allocs: tuple[Alloc, ...]
    units: int
    freight: Decimal
    transfer_cost: Decimal


@dataclass(frozen=True)
class Plan:
    shipments: tuple[Shipment, ...]
    delivery: date                                  # latest delivery across shipments
    freight: Decimal
    transfer_cost: Decimal
    notes: tuple[str, ...] = ()

    @property
    def parts(self) -> int:
        return len(self.shipments)

    @property
    def cost(self) -> Decimal:
        return self.freight + self.transfer_cost

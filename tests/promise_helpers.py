import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta, UTC
from decimal import Decimal

from limitara.core.config import Config
from limitara.core.promise.model import CapRow, Lane, Schedule, Snapshot, StockRow

TODAY = date(2026, 10, 31)  # a Saturday
CFG = Config(horizon_days=10)


def tiny(**over) -> Snapshot:
    cap = {}
    for off in range(10):
        day = TODAY + timedelta(days=off)
        cap[("L1", day)] = CapRow(0, 0, "closed") if day.weekday() == 6 else CapRow(10, 0, "open")
    weekdays = frozenset({0, 1, 2, 3, 4, 5})
    base = dict(
        now=datetime(2026, 10, 31, 5, 30, tzinfo=UTC), today=TODAY, now_ist_time=time(11, 0),
        cost={"X": Decimal("100")}, bom={"X": ("M", 1)},
        stock={("W1", "X"): StockRow(5, 0, TODAY.isoformat()), ("W2", "X"): StockRow(5, 0, TODAY.isoformat()),
               ("P1", "X"): StockRow(0, 0, TODAY.isoformat()), ("P1", "M"): StockRow(100, 0, TODAY.isoformat())},
        capacity=cap, line_plant={"L1": "P1"}, line_products={"L1": frozenset({"X"})},
        lanes={("P1", "W1"): Lane(1, Decimal("2")), ("P1", "W2"): Lane(2, Decimal("3"))},
        schedules={("W1", "C"): Schedule(weekdays, time(16, 0), 1, Decimal("100"), Decimal("1"), "A"),
                   ("W2", "C"): Schedule(weekdays, time(16, 0), 2, Decimal("150"), Decimal("1"), "B")},
        holidays=frozenset(), warehouses=frozenset({"W1", "W2"}), plants=frozenset({"P1"}), reads={},
    )
    base.update(over)
    return Snapshot(**base)


def random_snapshot(rng: random.Random, products=("X", "Y"), horizon=10, max_lines=2) -> Snapshot:
    plants = [f"P{i}" for i in range(rng.randint(1, 2))]
    whs = [f"W{i}" for i in range(rng.randint(2, 3))]
    lines = {f"L{p}{j}": p for p in plants for j in range(rng.randint(1, max_lines))}
    bom = {"X": ("MX", 1), "Y": ("MY", 2)}
    stock = {}
    for w in whs:
        for p in products:
            stock[(w, p)] = StockRow(rng.randint(0, 6), rng.randint(0, 2), TODAY.isoformat())
            stock[(w, p)] = StockRow(stock[(w, p)].on_hand, min(stock[(w, p)].reserved, stock[(w, p)].on_hand), TODAY.isoformat())
    for p in plants:
        for prod in products:
            stock[(p, prod)] = StockRow(rng.randint(0, 4), 0, TODAY.isoformat())
        for m in ("MX", "MY"):
            stock[(p, m)] = StockRow(rng.randint(0, 15), 0, TODAY.isoformat())
    cap = {}
    for line in lines:
        for off in range(horizon):
            day = TODAY + timedelta(days=off)
            roll = rng.random()
            if day.weekday() == 6 or roll < 0.1:
                cap[(line, day)] = CapRow(0, 0, "closed" if day.weekday() == 6 else "down")
            else:
                total = rng.randint(2, 8)
                cap[(line, day)] = CapRow(total, rng.randint(0, total), "open")
    lanes = {(p, w): Lane(rng.randint(1, 3), Decimal(rng.randint(1, 4))) for p in plants for w in whs if rng.random() < 0.85}
    sched = {}
    for w in whs:
        days = frozenset(rng.sample(range(0, 6), rng.randint(3, 6)))
        sched[(w, "C")] = Schedule(days, time(rng.randint(10, 18), 0), rng.randint(1, 3),
                                   Decimal(rng.randint(50, 200)), Decimal(rng.randint(1, 5)), "car")
    hols = frozenset(TODAY + timedelta(days=rng.randint(1, 8)) for _ in range(rng.randint(0, 2)))
    return Snapshot(
        now=datetime(2026, 10, 31, 5, 30, tzinfo=UTC), today=TODAY, now_ist_time=time(rng.randint(9, 20), 0),
        cost={p: Decimal("100") for p in products}, bom=bom, stock=stock, capacity=cap, line_plant=lines,
        line_products={l: frozenset(products) for l in lines}, lanes=lanes, schedules=sched, holidays=hols,
        warehouses=frozenset(whs), plants=frozenset(plants), reads={},
    )

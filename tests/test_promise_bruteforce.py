"""Measures how often the greedy search offers a later date than an exhaustive solver could.

Every plan must still verify (that is tested elsewhere). Here we only measure quality honestly:
the gap rate must stay below a stated tolerance, and the figures are printed with -s.
"""

import random
from datetime import timedelta
from itertools import combinations

from limitara.core.promise.model import Request
from limitara.core.promise.search import latest_ship, search_plans
from tests.promise_helpers import CFG, TODAY, random_snapshot

CITY = "C"


def brute_feasible(snap, origins, deadline, product, qty) -> bool:
    latest = {w: latest_ship(snap, w, CITY, deadline) for w in origins}
    if any(v is None for v in latest.values()):
        return False
    material, per = snap.bom[product]
    pools: dict = {}
    sources = []  # (warehouse, pool key, rm key or None)
    for w in origins:
        sched = snap.schedules[(w, CITY)]
        stock_from = snap.today if snap.now_ist_time < sched.cutoff else snap.today + timedelta(days=1)
        if stock_from <= latest[w] and (w, product) in snap.stock:
            pools[("s", w)] = snap.stock[(w, product)].available
            sources.append((w, ("s", w), None))
        for plant in sorted(snap.plants):
            lane = snap.lanes.get((plant, w))
            if lane is None:
                continue
            if snap.today + timedelta(days=lane.transit + CFG.handling_days) <= latest[w] and (plant, product) in snap.stock:
                pools[("ps", plant)] = snap.stock[(plant, product)].available
                sources.append((w, ("ps", plant), None))
            for line, lp in sorted(snap.line_plant.items()):
                if lp != plant:
                    continue
                for off in range(CFG.production_start_lag_days, CFG.horizon_days):
                    day = snap.today + timedelta(days=off)
                    row = snap.capacity.get((line, day))
                    if row and row.free > 0 and day + timedelta(days=1 + lane.transit + CFG.handling_days) <= latest[w]:
                        pools[("c", line, day)] = row.free
                        pools[("rm", plant)] = snap.stock[(plant, material)].available
                        sources.append((w, ("c", line, day), ("rm", plant)))

    def dfs(i, need, used):
        if i == len(sources):
            return need == 0 and used == set(origins)
        w, pool, rm = sources[i]
        top = min(need, pools[pool], pools[rm] // per if rm else need)
        for x in range(top, -1, -1):
            pools[pool] -= x
            if rm:
                pools[rm] -= x * per
            ok = dfs(i + 1, need - x, used | ({w} if x else set()))
            pools[pool] += x
            if rm:
                pools[rm] += x * per
            if ok:
                return True
        return False

    return dfs(0, qty, set())


def test_greedy_earliest_delivery_vs_exhaustive_earliest_delivery():
    rng = random.Random(11)
    cases = later = missed = 0
    total_days_late = 0
    for _ in range(60):
        snap = random_snapshot(rng, products=("X",), horizon=6, max_lines=1)
        qty = rng.randint(1, 4)
        plans = search_plans(snap, CFG, (("X", qty),), CITY)
        greedy_best = min((p.delivery for p in plans), default=None)
        served = sorted(w for w in snap.warehouses if (w, CITY) in snap.schedules)
        last = snap.today + timedelta(days=CFG.horizon_days + 7)
        brute_best, deadline = None, snap.today + timedelta(days=1)
        while deadline <= last and brute_best is None:
            if any(brute_feasible(snap, o, deadline, "X", qty) for n in range(1, len(served) + 1) for o in combinations(served, n)):
                brute_best = deadline
            deadline += timedelta(days=1)
        if brute_best is None and greedy_best is None:
            continue
        cases += 1
        if greedy_best is None:
            missed += 1
        elif greedy_best > brute_best:
            later += 1
            total_days_late += (greedy_best - brute_best).days
        assert greedy_best is None or brute_best is not None, "greedy found a plan that exhaustive search says is impossible"
        assert greedy_best is None or greedy_best >= brute_best
    print(f"\nrequests with a feasible answer: {cases}; greedy later: {later} ({total_days_late} days in total); missed: {missed}")
    assert cases > 25
    assert (later + missed) / cases < 0.10

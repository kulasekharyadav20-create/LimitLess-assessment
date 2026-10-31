# Promises the Factory Can Keep

Quote-to-dispatch for a flange maker. Every date, price and quantity the system commits to is checked against live
capacity, stock and price records, and nothing reaches a customer without the right person's approval.

## Run it (one command)

```bash
uv run start
```

That rebuilds the seed data, starts the five stand-ins and Core, waits for each to be healthy, and prints the addresses.
Press Ctrl-C to stop all six. No `uv`? Use `pip install -e . && python -m limitara.launcher`.

| Program | Address | What it is |
|---|---|---|
| Mailbox | http://127.0.0.1:4001 | email threads, attachments |
| CRM | http://127.0.0.1:4002 | accounts, terms, credit, staff |
| ERP | http://127.0.0.1:4003 | stock, price lists, quotes, orders, reservations |
| Production | http://127.0.0.1:4004 | capacity per line per day, bookings, work orders |
| Dispatch | http://127.0.0.1:4005 | carrier schedules, cut-offs, freight, shipments |
| **Core** | **http://127.0.0.1:8400** | **the system: approvals, rep and customer views** |

Every stand-in also serves `/docs` (its interactive API page). If a port is busy the launcher says which one and exits.
Override ports with `LIMITARA_PORT_BASE` and `LIMITARA_CORE_PORT`. Pin the clock for every process with
`LIMITARA_FIXED_NOW=2026-10-31T05:30:00+00:00` (otherwise real time is used).

## Test it

```bash
uv run pytest                 # ~290 tests, about 25 seconds, needs no running system
uv run pytest tests/test_principles.py tests/test_promise.py -v     # the five principles and the promise engine
uv run sample-run             # fifteen enquiries through the real system; writes docs/sample-run.md
uv run seed                   # reset all stand-in data (the launcher does this on every start)
```

| Principle | Where it is tested |
|---|---|
| 1. Never promise what cannot be kept | `test_promise.py` (verifier on 150 random factories, tamper tests), `test_promise_bruteforce.py`, `test_orders.py` |
| 2. Traceable, stale data flagged | `test_core_foundation.py`, `test_freshness.py`, `test_pricing_router.py`, `test_core_app.py` (basis on every screen) |
| 3. Mail is evidence, not instruction | `test_interpreter.py`, `test_intake.py`, `test_principles.py` (no email path to a price) |
| 4. Duplicates, ordering, old quotes | `test_intake.py`, `test_orders.py` (one order per quote version, crash resume), `test_erp_api.py` |
| 5. Say "I don't know" and route it | `test_core_foundation.py`, `test_intake.py`, `test_sample_run.py` |

## Try it by hand (about five minutes)

Run `uv run start` in one terminal and these in another. Sign-in is a stub: send `X-User: <id>`, or open
http://127.0.0.1:8400 in a browser and choose a person. People: `REP-01` Meera (rep), `MGR-01` Sanjay (sales manager),
`HEAD-01` Divya (sales head), `FIN-01` Rohit (finance), `CT-101` Rakesh (customer, Shree Balaji), `CT-201` Vikram (Konkan).

```bash
# 1. the customer's enquiry arrives (the system reads new mail every 3 seconds)
curl -s -X POST localhost:4001/inbound -H 'content-type: application/json' -d '{
  "message_id":"<demo1@x>","thread_id":"T-DEMO","customer_id":"CUST-001","from_addr":"rakesh.shah@balajisupplies.example",
  "body":"We need 300 of the 50mm flange and 120 of the 80mm flange, both to our Pune warehouse, before the 14th. Send a price that holds if we confirm by Friday."}'

# 2. the approver sees it, with the reasons it needs a person (or open /ui/approvals in a browser as Sanjay)
curl -s localhost:8400/api/approvals -H 'X-User: MGR-01'
curl -s localhost:8400/api/approvals/Q-2026-0002/1 -H 'X-User: MGR-01'     # prices, cost, margin, plan, basis
curl -s -X POST localhost:8400/api/approvals/Q-2026-0002/1/approve -H 'X-User: MGR-01' -H 'content-type: application/json' -d '{"reason":"ok"}'

# 3. the customer accepts, quoting the version
curl -s -X POST localhost:4001/inbound -H 'content-type: application/json' -d '{
  "message_id":"<demo2@x>","thread_id":"T-DEMO","customer_id":"CUST-001","from_addr":"rakesh.shah@balajisupplies.example",
  "body":"Confirmed, go ahead. Re: Quotation Q-2026-0002 v1"}'

# 4. status for the customer, and for the rep
curl -s localhost:8400/api/customer/orders -H 'X-User: CT-101'
curl -s localhost:8400/api/rep/orders      -H 'X-User: REP-01'

# 5. production slips: the rep is alerted with verified options, the customer's date is hidden until a person decides
curl -s "localhost:4004/work-orders?order_ref=SO-1001"          # note a work order id and its promised_date
curl -s -X PATCH localhost:4004/admin/work-orders/WO-1008 -H 'content-type: application/json' -d '{"forecast_finish":"2026-11-12","state":"late","note":"gearbox repair"}'
curl -s -X POST localhost:8400/admin/check-slips -H 'X-User: REP-01'
curl -s localhost:8400/api/rep/alerts -H 'X-User: REP-01'

# 6. change one requirement in the room (nothing is written)
curl -s -X POST localhost:8400/api/whatif/promise -H 'X-User: REP-01' -H 'content-type: application/json' -d '{
  "lines":[["FL-50",300],["FL-80",120]],"city":"Pune","deliver_by":"2026-11-13","partial":"ask",
  "change":{"kind":"plant_down","plant":"PLT-A","from":"2026-11-02","to":"2026-11-12"}}'
curl -s -X POST localhost:8400/api/whatif/floor -H 'X-User: REP-01' -H 'content-type: application/json' -d '{"quotation_id":"Q-6001","version":2,"floor_pct":"9"}'
```

The seeded traps run by themselves on start: the "skip approval" email, the duplicate purchase order whose attachment
disagrees, the late "confirmed" that quotes a superseded version, the credit hold, and the open freight dispute.
Look at `GET /admin/status` and the rep page for the tasks they produce.

## Duplicates and out-of-order mail

- **Doubtful duplicate enquiry** (same customer, same products, quantities within 25%, same city, within 24h of an open case):
  nothing is quoted and the message is *held*. The rep's task has two buttons: **Duplicate: discard** or **New request: proceed**
  (also `POST /api/tasks/{id}/duplicate/{discard|proceed}`). Proceed quotes it once; a second click is refused.
- **Acceptance when a newer message exists:** if the thread shows a later-sent customer message, the older acceptance is simply
  overtaken and ignored; no person is needed (the newer message is handled on its own merits).
- **Acceptance when "latest" cannot be proven** (arrived over 2h after it was sent, send time unreadable or ahead of arrival,
  or two messages with the same send time): no order; the rep is told to ask the customer. Limits are in `core/config.py`.

## Pages (server-rendered)

`/ui/approvals` (approvers) · `/ui/approvals/{quote}/{version}` (reasons, cost, margin, plan, basis; approve, change, reject) ·
`/ui/rep` (cases, tasks, alerts) · `/ui/rep/orders/{order}` (production, shipments, slip options and decision) ·
`/ui/customer` and `/ui/customer/orders/{order}` (what a customer may see).

## Where the code is

```
src/limitara/
  money.py clock.py freshness.py calendar.py hashing.py quote_states.py   exact money, one clock, rules shared everywhere
  standins/<name>/store.py (schema + seed) and app.py (the service)        five separate processes with their own SQLite file
  core/promise/        search.py select.py verify.py explain.py            the promise engine (pure) and its independent verifier
  core/pricing/ core/approvals/ core/intake/interpreter.py                 price, flags, who approves, reading mail (pure)
  core/quotes/ core/outbound/gate.py core/orders/saga.py core/monitor/     lifecycle, the only door to customers, order saga, slips
  core/views/ core/app.py                                                  allow-list views, API and pages
docs/design-note.md  docs/sample-run.md                                    the three-page design note, the fifteen-enquiry run
```

## Where this differs from the written low-level design

- Core listens on **8400**, not 3000 (3000 is often taken).
- A customer reply that arrives **after the quote expired** is sent to a person even if it was sent in time, because ERP only
  allows `sent -> accepted`. (The design preferred judging by the customer's send time.)
- **3000 x FL-50** produces no plan within the planning horizon, so the rep gets a task rather than "alternatives". Alternatives
  appear whenever any plan exists (see enquiry 13 and the unit tests).
- **Order amendments are not built** (cancellation is). The change-request path creates a task for the rep.
- Reps see a floor flag without its margin numbers; approvers see them.
- Authentication is a stub (choose a person); authorisation is real. No authentication between the six programs.

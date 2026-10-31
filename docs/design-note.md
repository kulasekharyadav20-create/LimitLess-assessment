# Design note: Promises the Factory Can Keep

## 1. How a promise is computed

Core takes one **snapshot**: stock and reservations, capacity per line per day, steel, lanes, carrier schedules and
holidays, each read stamped with its time. A stale read is re-read once; if it is still stale, or a stand-in is down, the
result is *unknown* and goes to a person. A missing capacity day is never assumed to be free.

A pure function then searches for plans. A flange can come from **stock in a warehouse** (ships at the next carrier day, today
only if the cut-off has not passed), **finished stock at a plant** (plus transfer and one handling day), or **new production**
on a line that can make it, limited by free line-units that day and by steel (bill of materials). Production is made from
tomorrow and is available the day after it is made. Delivery never lands on a Sunday or holiday. Each warehouse ships at most
once, so every extra shipment is visible as an extra base freight charge. The search tries every group of warehouses and the
earliest deadline each can meet, filling greedily from one shared ledger so no unit, line-day or steel is counted twice.

**Selection** follows the customer's terms. "Parts not allowed" offers only single-shipment plans. "Ask" (Shree Balaji)
offers a single shipment if one meets the date; otherwise the best split, marked *needs the customer's consent*, with the
extra freight in rupees. If the date cannot be met the result contains only labelled alternatives (single, later; split,
earliest; part now and the rest later). It never labels an unachievable date as achievable.

**Every plan is checked by a separate verifier** that recomputes each claim from the raw snapshot: stock, line-units and steel
within what is free, legal carrier day before cut-off, correct delivery date and freight, exact coverage. It runs before a
plan is shown, before sending, and again at acceptance, with the quote's own holds added back. Each plan is stored with a
basis: every record it used, its values and its read time, plus what limited it.

## 2. How prices are set and approved

The price comes from the list valid today (the customer's own list first), the terms valid today, a rep discount if a signed-in
rep typed one, exact `Decimal` arithmetic rounded half-up to paise, and a floor on margin over selling price (12%, or the
customer's override). Expired terms or lists are never applied silently: they raise a flag. A price that falls below the
floor is never "fixed" by the system; the flag gives the margin and the lowest compliant price (rounded up, so it really passes).
The offer's validity is capped at the end of the list's last day and says so.

Every quote waits for a person. A router chooses who from the flags and each person's margin authority in CRM: a sales manager
(down to 9%), a sales head (down to 5%), nobody below that, and finance for any credit flag. The creator of a version can
never approve it. An approver can approve, reject (with a reason), or change, and a change is a **new version** that someone
else approves; they can choose among verified plans but cannot type a date. All outgoing customer mail passes one Outbound
Gate that checks approvals, a content hash and plan freshness. Email text has no route to a price: the pricing function's
only discount input is set by a signed-in rep.

## 3. Partial orders, changed orders, slips

The split is decided when the plan is made and is shown, with the freight consequence, to the approver and in the quote. If
the plan the customer accepted no longer holds at acceptance, the system makes **no order** and prepares a new version rather
than silently splitting. An order is created by a sequence of idempotent steps (accept, ERP order, convert holds, work orders,
transfers, shipments, confirm) that is undone in reverse if any step is refused and resumed after a crash; one quote version
can never produce two orders. Cancellation releases everything not yet started. Order amendments are not built.

A poll every 60 seconds compares Production's forecast with each work order's latest acceptable finish. A slip alerts the rep
at once with options that are plans true right now. The customer's view stops showing the affected date, and **a person decides**
whether and what to tell them. A notice stating a new date also needs a sales manager.

## 4. Automatic versus a person

| Automatic | Waits for a person |
|---|---|
| Reading and de-duplicating mail; matching customers | Approving, changing or rejecting a quote; any discount |
| Snapshots, promises, prices, flags, routing, verification | Credit exceptions; consent to a split |
| Soft holds (expire with the quote) and releasing them | Ambiguous dates, conflicting emails, unknown senders or products |
| Order creation after a valid acceptance of an approved quote, with undo | Late, superseded, unauthorised or changed acceptances |
| Order confirmation identical to the approved quote | Whether and what the customer is told about a slip |
| Slip detection, alerts, retries, recovery after a crash | Anything that fails repeatedly (becomes `stuck`) |

## 5. What I chose not to build

An AI model for reading mail (rules are repeatable and safe; the interpreter interface allows one); order amendments; changeover
times and per-product rates; incoming steel; warehouse-to-warehouse transfers; tax and multi-currency; carrier delay tracking after
dispatch; a free-text composer for customers (templates only, so every commitment comes from verified data); real
authentication; a polished UI; an optimal plan search (it guarantees a *correct* plan, not the best).

## 6. The decision I am least sure about

The greedy plan search. The verifier guarantees every plan it returns is true, but greedy filling can offer a later date than
exists, for example when two warehouses compete for one production slot. **What would prove it wrong:** the brute-force test in
`tests/test_promise_bruteforce.py` finds an earlier feasible date than the search on more than a few percent of small factories
(on 60 random small factories it found none), or a plan the verifier rejects. Because the verifier is separate, replacing the
search with an exact one would not weaken the guarantee.

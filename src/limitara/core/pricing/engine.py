"""Price a request from the customer's current terms and the price list valid today.

Never invents a number: it either returns a price with its full working or raises a flag.
Email text has no way to reach this function; only a logged-in rep can supply a discount.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, UTC
from decimal import Decimal

from limitara.clock import IST
from limitara.core.config import Config
from limitara.core.pricing.flags import Flag, money
from limitara.money import apply_discount, floor_price, line_total, margin_pct, meets_floor


@dataclass(frozen=True)
class PriceListInfo:
    id: str
    customer_id: str | None
    valid_from: date
    valid_to: date
    prices: dict[str, Decimal]


@dataclass(frozen=True)
class TermsInfo:
    id: str
    discount_pct: Decimal
    valid_from: date
    valid_to: date
    partial: str
    floor_override: Decimal | None


@dataclass(frozen=True)
class PricingInputs:
    customer_id: str
    today: date
    lists: tuple[PriceListInfo, ...]
    terms: tuple[TermsInfo, ...]
    cost: dict[str, Decimal]
    lines: tuple[tuple[str, int], ...]
    asked_valid_until: datetime
    rep_discount_pct: Decimal = Decimal(0)
    reads: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceLine:
    product: str
    qty: int
    status: str                      # ok | below_floor | cannot_price
    list_price: Decimal | None = None
    price_list_id: str | None = None
    terms_discount_pct: Decimal = Decimal(0)
    rep_discount_pct: Decimal = Decimal(0)
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    cost: Decimal | None = None
    margin_pct: Decimal | None = None
    floor_pct: Decimal | None = None
    floor_price: Decimal | None = None


@dataclass(frozen=True)
class PriceResult:
    lines: tuple[PriceLine, ...]
    total: Decimal
    valid_until: datetime
    flags: tuple[Flag, ...]
    terms_id: str | None = None
    partial: str = "allowed"


def _valid(day_from: date, day_to: date, today: date) -> bool:
    return day_from <= today <= day_to


def _end_of_day_utc(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=IST).astimezone(UTC)


def price_request(inp: PricingInputs, cfg: Config) -> PriceResult:
    flags: list[Flag] = []
    t = inp.today

    live_terms = next((x for x in inp.terms if _valid(x.valid_from, x.valid_to, t)), None)
    if live_terms is None and inp.terms:
        lapsed = max(inp.terms, key=lambda x: x.valid_to)
        flags.append(Flag("TERMS_EXPIRED",
                          f"Agreed terms ({lapsed.discount_pct}% off) ended on {lapsed.valid_to.isoformat()}. "
                          "No discount has been applied; an approver decides whether to honour them.",
                          provenance=({"source": "crm", "record": f"terms:{lapsed.id}", "read_at": inp.reads.get("crm"),
                                       "values": {"discount_pct": str(lapsed.discount_pct), "valid_to": lapsed.valid_to.isoformat()}},)))
    elif live_terms is None:
        flags.append(Flag("NO_TERMS", "No agreed terms exist for this customer, so list price applies with no discount."))
    terms_pct = live_terms.discount_pct if live_terms else Decimal(0)
    floor = live_terms.floor_override if live_terms and live_terms.floor_override is not None else cfg.default_floor_pct

    own_lists = [x for x in inp.lists if x.customer_id == inp.customer_id]
    std_lists = [x for x in inp.lists if x.customer_id is None]
    expired_own_flagged = False
    out: list[PriceLine] = []
    list_end: date | None = None

    for product, qty in inp.lines:
        chosen = next((x for x in own_lists if _valid(x.valid_from, x.valid_to, t) and product in x.prices), None)
        if chosen is None:
            chosen = next((x for x in std_lists if _valid(x.valid_from, x.valid_to, t) and product in x.prices), None)
            if chosen is not None and own_lists and not expired_own_flagged:
                expired_own_flagged = True
                lapsed = max(own_lists, key=lambda x: x.valid_to)
                flags.append(Flag("AGREED_LIST_EXPIRED",
                                  f"The customer's own price list {lapsed.id} ended on {lapsed.valid_to.isoformat()}. "
                                  f"The standard list {chosen.id} was used instead, which may differ from what was agreed."))
        cost = inp.cost.get(product)
        if chosen is None or cost is None:
            why = "no price list is valid today" if chosen is None else "no cost record exists"
            flags.append(Flag("CANNOT_PRICE", f"{product}: {why}, so no price can be set."))
            out.append(PriceLine(product, qty, "cannot_price"))
            continue
        list_end = chosen.valid_to if list_end is None else min(list_end, chosen.valid_to)
        listed = chosen.prices[product]
        unit = apply_discount(listed, terms_pct) if terms_pct > 0 else listed
        if inp.rep_discount_pct > 0:
            unit = apply_discount(unit, inp.rep_discount_pct)
        margin = margin_pct(unit, cost)
        ok = meets_floor(unit, cost, floor)
        low = floor_price(cost, floor)
        out.append(PriceLine(product, qty, "ok" if ok else "below_floor", listed, chosen.id, terms_pct, inp.rep_discount_pct,
                             unit, line_total(unit, qty), cost, margin, floor, low))
        if not ok:
            terms_alone = inp.rep_discount_pct == 0 and terms_pct > 0
            code = "TERMS_BREACH_FLOOR" if terms_alone else "BELOW_FLOOR"
            why = f"Agreed terms ({terms_pct}% off) put this line below the floor. " if terms_alone else ""
            flags.append(Flag(code,
                              f"{product}: unit price {money(unit)} gives a margin of {margin.quantize(Decimal('0.01'))}%, below the "
                              f"{floor.quantize(Decimal('0.01'))}% floor. {why}Lowest price meeting the floor: {money(low)}.",
                              margin_pct=margin,
                              provenance=({"source": "erp", "record": f"price_list:{chosen.id}:{product}", "read_at": inp.reads.get("erp"),
                                           "values": {"list_price": str(listed)}},
                                          {"source": "erp", "record": f"item:{product}", "read_at": inp.reads.get("erp"),
                                           "values": {"unit_cost": str(cost)}})))

    valid_until = inp.asked_valid_until
    if list_end is not None:
        cap = _end_of_day_utc(list_end)
        if cap < valid_until:
            valid_until = cap
            flags.append(Flag("VALIDITY_SHORTENED",
                              f"The price list ends on {list_end.isoformat()}, before the validity asked for. "
                              f"The offer is limited to {list_end.isoformat()} (India time) and says so."))
    total = sum((ln.line_total for ln in out if ln.line_total is not None), Decimal(0))
    return PriceResult(tuple(out), total, valid_until, tuple(flags), live_terms.id if live_terms else None,
                       live_terms.partial if live_terms else "allowed")


def credit_flags(credit_status: str, credit_limit: Decimal, outstanding: Decimal, total: Decimal) -> list[Flag]:
    out: list[Flag] = []
    if credit_status == "hold":
        out.append(Flag("CREDIT_HOLD", "The customer's account is on credit hold. Finance must approve any quote."))
    headroom = credit_limit - outstanding
    if total > headroom:
        out.append(Flag("CREDIT_HEADROOM_LOW",
                        f"The quote total {money(total)} exceeds the remaining credit of {money(max(headroom, Decimal(0)))}."))
    return out

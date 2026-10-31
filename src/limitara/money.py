"""Exact money arithmetic. All amounts are INR, ex-tax, and rounded to paise.

Rules (see design note):
- Never float. Amounts cross process boundaries as strings.
- A discounted price rounds HALF-UP to paise.
- A floor price (lowest price that meets the margin floor) rounds UP, so that
  rounding can never leave us under the floor.
- Margin is profit divided by selling price, not by cost.
- Percentages are plain percent numbers: Decimal("12") means 12%.
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

PAISE = Decimal("0.01")
HUNDRED = Decimal(100)


class MoneyError(ValueError):
    """Raised for input that must never be turned into a price."""


def to_money(value: str | int | Decimal) -> Decimal:
    """Parse an amount without ever passing through float."""
    if isinstance(value, float) or isinstance(value, bool):
        raise MoneyError(f"money must be str, int or Decimal, got {type(value).__name__}")
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise MoneyError(f"not a valid amount: {value!r}") from exc
    if not amount.is_finite():
        raise MoneyError(f"amount must be finite: {value!r}")
    return amount


def round_paise(amount: Decimal) -> Decimal:
    """Round half-up to paise. The rule for every customer-facing price."""
    return amount.quantize(PAISE, rounding=ROUND_HALF_UP)


def to_wire(amount: Decimal) -> str:
    """Serialise for JSON. Always two decimals, always a string."""
    return str(amount.quantize(PAISE, rounding=ROUND_HALF_UP))


def _require_price(price: Decimal, name: str = "price") -> None:
    if price <= 0:
        raise MoneyError(f"{name} must be positive, got {price}")


def _require_pct(pct: Decimal, name: str) -> None:
    if pct < 0 or pct >= HUNDRED:
        raise MoneyError(f"{name} must be at least 0 and below 100, got {pct}")


def apply_discount(price: Decimal, discount_pct: Decimal) -> Decimal:
    """Unit price after a percentage discount, rounded half-up to paise."""
    _require_price(price)
    _require_pct(discount_pct, "discount")
    return round_paise(price * (HUNDRED - discount_pct) / HUNDRED)


def margin_pct(price: Decimal, cost: Decimal) -> Decimal:
    """Margin on price, as a percent (unrounded). Negative if selling below cost."""
    _require_price(price)
    _require_price(cost, "cost")
    return (price - cost) / price * HUNDRED


def meets_floor(price: Decimal, cost: Decimal, floor_pct: Decimal) -> bool:
    """True if margin on price is at or above the floor. Exact: no division."""
    _require_price(price)
    _require_price(cost, "cost")
    _require_pct(floor_pct, "margin floor")
    return (price - cost) * HUNDRED >= floor_pct * price


def floor_price(cost: Decimal, floor_pct: Decimal) -> Decimal:
    """Lowest unit price, in whole paise, that still meets the margin floor."""
    _require_price(cost, "cost")
    _require_pct(floor_pct, "margin floor")
    exact = cost * HUNDRED / (HUNDRED - floor_pct)
    return exact.quantize(PAISE, rounding=ROUND_CEILING)


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    """Line total is always the rounded unit price times quantity."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise MoneyError(f"quantity must be a positive integer, got {quantity!r}")
    return unit_price.quantize(PAISE) * quantity

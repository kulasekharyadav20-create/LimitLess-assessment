"""Customer-facing quote text, generated from data by a fixed template. Never contains cost or margin."""

from datetime import datetime
from decimal import Decimal

from limitara.clock import IST


def money(d: str | Decimal) -> str:
    return f"Rs {Decimal(str(d)):,.2f}"


def render_quote(quotation_id: str, version: int, customer_name: str, lines: list[dict], shipments: list[dict],
                 valid_until: datetime, interpretation_note: str, total: str, notes: list[str],
                 single_comparison: dict | None) -> tuple[str, str]:
    subject = f"Quotation {quotation_id} v{version}"
    out = [f"Dear {customer_name},", "", f"Quotation {quotation_id} v{version}", ""]
    for ln in lines:
        out.append(f"  {ln['qty']} x {ln['product_id']} at {money(ln['unit_price'])} each "
                   f"= {money(Decimal(ln['unit_price']) * ln['qty'])} (delivery by {ln['promised_delivery_date']})")
    out.append(f"  Total (excluding tax): {money(total)}")
    out += ["", "Delivery plan:"]
    for i, sh in enumerate(shipments, start=1):
        out.append(f"  Shipment {i}: {sh['units']} units from {sh['warehouse']}, ships {sh['ship_date']}, "
                   f"delivered {sh['delivery_date']}, freight {money(sh['freight'])}")
    if len(shipments) > 1:
        out.append(f"  This plan ships in {len(shipments)} parts.")
        if single_comparison:
            out.append(f"  One shipment would deliver on {single_comparison['delivery']} for freight {money(single_comparison['freight'])}; "
                       f"the extra freight for this plan is {money(single_comparison['extra'])}.")
    for n in notes:
        out.append(f"  Note: {n}")
    if interpretation_note:
        out += ["", f"How we read your request: {interpretation_note}"]
    out += ["", f"This offer is valid until {valid_until.astimezone(IST).strftime('%A %d %B %Y, %H:%M')} India time.",
            f"To accept, reply quoting {quotation_id} v{version}."]
    return subject, "\n".join(out)

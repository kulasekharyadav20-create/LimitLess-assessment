"""Content hash of what a customer is told. Two quotes with the same hash say the same thing."""

import hashlib
import json


def content_hash(lines: list[dict], shipments: list[dict], valid_until: str, note: str, currency: str = "INR") -> str:
    canonical = {
        "lines": sorted(
            (
                {
                    "product_id": ln["product_id"],
                    "qty": int(ln["qty"]),
                    "unit_price": str(ln["unit_price"]),
                    "promised_delivery_date": str(ln.get("promised_delivery_date") or ""),
                }
                for ln in lines
            ),
            key=lambda d: d["product_id"],
        ),
        "shipments": sorted(
            (
                {
                    "warehouse": s["warehouse"],
                    "ship_date": str(s["ship_date"]),
                    "delivery_date": str(s["delivery_date"]),
                    "units": int(s["units"]),
                    "freight": str(s["freight"]),
                }
                for s in shipments
            ),
            key=lambda d: (d["warehouse"], d["ship_date"]),
        ),
        "valid_until": valid_until,
        "note": note or "",
        "currency": currency,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

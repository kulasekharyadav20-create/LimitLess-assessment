"""The stage shown to people, calculated from the records and never typed by hand."""


def order_stage(order_state: str, work_orders: list[dict], shipments: list[dict], at_risk: bool = False) -> str:
    if order_state in ("failed", "cancelled", "amending", "creating"):
        return order_state
    customer = [s for s in shipments if s.get("kind", "customer") == "customer" and s["status"] != "cancelled"]
    live_wo = [w for w in work_orders if w["state"] != "cancelled"]
    if customer and all(s["status"] == "delivered" for s in customer):
        return "delivered"
    if customer and all(s["status"] in ("dispatched", "delivered") for s in customer):
        return "dispatched"
    if customer and any(s["status"] in ("dispatched", "delivered") for s in customer):
        return "part_dispatched"
    if live_wo and all(w["state"] == "done" for w in live_wo):
        return "ready"
    if any(w["state"] in ("in_progress", "late", "done") for w in live_wo):
        return "in_production"
    return "confirmed"

from collections import defaultdict
from decimal import Decimal


def historical_service(catalog):
    totals = defaultdict(lambda: {"sold_kg": 0.0, "known_requested_kg": 0.0, "missing_requests": 0})
    for record in catalog.rows("demand"):
        group = totals[record["sku"]]
        group["sold_kg"] += record["sold_kg"]
        if record["requested_kg"] is None:
            group["missing_requests"] += 1
        else:
            group["known_requested_kg"] += record["requested_kg"]
    return dict(totals)


def overview(catalog):
    return {"起点库存公斤": sum(record["quantity_kg"] for record in catalog.rows("inventory")), "历史履约描述": historical_service(catalog), "供应商日数": len(catalog.rows("supplier_days"))}

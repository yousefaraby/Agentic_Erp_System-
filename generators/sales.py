"""Sales generators.

``generate_demand`` builds sales orders and order items (demand) and records
aggregate demand per (warehouse, product) for the procurement planner.

After the inventory ledger simulates fulfillment, ``build_after_ledger``
produces shipments, payments and returns that agree with the simulated outcome.
"""
from __future__ import annotations

import bisect
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .base import World, money, rand_dt

CHANNELS = ["in_store", "online", "phone", "wholesale"]
CHANNEL_WEIGHTS = [45, 35, 5, 15]

CARRIERS = ["SwiftShip", "GlobalFreight", "MetroCourier", "ParcelPro", "TransLogic"]
PAY_METHODS = ["cash", "card", "bank_transfer", "mobile_money", "credit"]
RETURN_REASONS = ["defective", "wrong_item", "not_as_described", "changed_mind",
                  "damaged_in_transit", "late_delivery", "other"]

SEASON_MONTHS = {
    "summer": {6: 1.5, 7: 1.7, 8: 1.4},
    "winter": {12: 1.4, 1: 1.5, 2: 1.3},
    "holiday": {11: 1.8, 12: 2.2},
    "back_to_school": {8: 1.6, 9: 1.5},
    "none": {},
}


def _season_mult(seasonality: str, month: int) -> float:
    return SEASON_MONTHS.get(seasonality, {}).get(month, 1.0)


def _sample_datetime(rng, start, end):
    """Sample a timestamp with weekend weighting and business hours."""
    days = (end.date() - start.date()).days
    while True:
        d = start.date() + timedelta(days=rng.randint(0, max(days, 0)))
        w = 1.4 if d.weekday() >= 5 else 1.0
        if rng.random() < w / 1.4:
            break
    t = time(hour=rng.randint(8, 20), minute=rng.randint(0, 59))
    return datetime.combine(d, t, tzinfo=timezone.utc)


def _weighted_sample(rng, ids, weights, k):
    """Sample ``k`` distinct ids using the given weights."""
    pool = list(zip(ids, weights))
    chosen = []
    k = min(k, len(pool))
    for _ in range(k):
        total = sum(w for _, w in pool)
        r = rng.random() * total
        upto = 0.0
        for idx, (item_id, w) in enumerate(pool):
            upto += w
            if upto >= r:
                chosen.append(item_id)
                pool.pop(idx)
                break
        else:
            chosen.append(pool.pop()[0])
    return chosen


def generate_demand(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    tax_rate = cfg.default_tax_rate

    # branch performance factors -> differences between branches
    world.branch_factor = {}
    for b in world.branches:
        world.branch_factor[b["branch_id"]] = rng.uniform(0.7, 1.35)

    # precompute per (warehouse, month) choice weights
    wh_month_weights: dict[tuple[int, int], tuple[list[int], list[float]]] = {}
    for wh in world.warehouses:
        wh_id = wh["warehouse_id"]
        for month in range(1, 13):
            ids, weights = [], []
            for p in world.products:
                key = (wh_id, p["product_id"])
                stocked = world.assortment.get(key, False)
                d = world.product_demand[p["product_id"]]
                w = d["base_daily"] * _season_mult(d["seasonality"], month)
                w *= 1.0 if stocked else 0.02
                ids.append(p["product_id"])
                weights.append(w)
            wh_month_weights[(wh_id, month)] = (ids, weights)

    world.demand_by_wh_prod: dict[tuple[int, int], int] = {}
    world.demand_by_wh_month: dict[int, list[int]] = {}
    for wh in world.warehouses:
        world.demand_by_wh_month[wh["warehouse_id"]] = [0] * 12
    world.order_warehouse: dict[int, int] = {}
    branch_ids = [b["branch_id"] for b in world.branches]
    branch_w = [world.branch_factor[b] for b in branch_ids]
    max_items = cfg.max_items_per_order

    for _ in range(cfg.n_sales_orders):
        order_dt = _sample_datetime(rng, datetime.combine(cfg.history_start, time.min, tzinfo=timezone.utc),
                                    datetime.combine(cfg.history_end, time.max, tzinfo=timezone.utc))
        month = order_dt.month

        branch = rng.choices(world.branches, weights=branch_w, k=1)[0]
        wh = world.branch_warehouse[branch["branch_id"]]
        wh_id = wh["warehouse_id"]

        customer = rng.choices(world.customers,
                               weights=[2 if c["loyalty_tier"] in ("gold", "platinum") else 1
                                        for c in world.customers], k=1)[0]

        weekend = order_dt.weekday() >= 5
        chan_weights = list(CHANNEL_WEIGHTS)
        if weekend:
            chan_weights[0] += 15
            chan_weights[1] += 5
        channel = rng.choices(CHANNELS, weights=chan_weights, k=1)[0]

        n_items = rng.choices(range(1, max_items + 1),
                              weights=[max_items - i + 1 for i in range(1, max_items + 1)],
                              k=1)[0]

        ids, weights = wh_month_weights[(wh_id, month)]
        chosen_products = _weighted_sample(rng, ids, weights, n_items)

        cancelled = rng.random() < cfg.cancel_rate
        status = "pending"  # provisional; the ledger finalises it

        order_id = world.next_id("sales_orders")
        world.order_warehouse[order_id] = wh_id

        order = {
            "sales_order_id": order_id,
            "order_number": f"SO-{order_dt.year}{order_dt.month:02d}-{order_id:07d}",
            "branch_id": branch["branch_id"],
            "customer_id": customer["customer_id"],
            "employee_id": rng.choice(
                [e for e in world.employees
                 if e["branch_id"] == branch["branch_id"] and e["department"] == "Sales"]
                or [e for e in world.employees if e["branch_id"] == branch["branch_id"]]
                or world.employees
            )["employee_id"],
            "order_date": order_dt,
            "status": "cancelled" if cancelled else status,
            "channel": channel,
            "subtotal": Decimal("0"),
            "discount_amount": Decimal("0"),
            "tax_amount": Decimal("0"),
            "shipping_amount": Decimal("0"),
            "total_amount": Decimal("0"),
            "currency": cfg.currency,
            "notes": None,
            "cancelled_at": (order_dt + timedelta(hours=rng.randint(1, 48))) if cancelled else None,
            "created_at": order_dt,
            "updated_at": order_dt,
        }

        subtotal = Decimal("0")
        discount_total = Decimal("0")
        tax_total = Decimal("0")
        line_no = 0
        for product_id in chosen_products:
            line_no += 1
            product = world.product_by_id[product_id]
            quantity = rng.choices([1, 2, 3, 4, 5, 6, 8],
                                   weights=[40, 25, 14, 8, 5, 4, 2], k=1)[0]
            unit_price = product["list_price"]
            # occasional promotional discount
            discount_rate = Decimal("0")
            r = rng.random()
            if r < 0.12:
                discount_rate = Decimal(str(round(rng.uniform(0.05, 0.30), 4)))
            elif r < 0.20:
                discount_rate = Decimal(str(round(rng.uniform(0.30, 0.45), 4)))

            line_subtotal = money(unit_price * quantity)
            line_discount = money(line_subtotal * discount_rate)
            net = line_subtotal - line_discount
            line_tax = money(net * tax_rate)
            line_total = net + line_tax

            world.add("sales_order_items", {
                "sales_order_item_id": world.next_id("sales_order_items"),
                "sales_order_id": order_id,
                "line_number": line_no,
                "product_id": product_id,
                "warehouse_id": wh_id,
                "quantity": quantity,
                "quantity_fulfilled": 0,
                "unit_price": unit_price,
                "discount_rate": discount_rate,
                "tax_rate": tax_rate,
                "line_subtotal": line_subtotal,
                "line_total": line_total,
                "created_at": order_dt,
            })
            subtotal += line_subtotal
            discount_total += line_discount
            tax_total += line_tax

            if not cancelled:
                key = (wh_id, product_id)
                world.demand_by_wh_prod[key] = world.demand_by_wh_prod.get(key, 0) + quantity
                world.demand_by_wh_month[wh_id][month - 1] += quantity

        if channel == "in_store":
            shipping = Decimal("0")
        else:
            shipping = money(Decimal(rng.choice([0, 5, 7, 9, 12, 15, 20])))
            if subtotal > Decimal("150"):
                shipping = Decimal("0")

        order["subtotal"] = subtotal
        order["discount_amount"] = discount_total
        order["tax_amount"] = tax_total
        order["shipping_amount"] = shipping
        order["total_amount"] = subtotal - discount_total + tax_total + shipping

        world.add("sales_orders", order)
        world.sales_orders.append(order)

    world.rows["sales_orders"].sort(key=lambda r: r["sales_order_id"])
    world.rows["sales_order_items"].sort(key=lambda r: r["sales_order_item_id"])


# ---------------------------------------------------------------------------
# Post-ledger records
# ---------------------------------------------------------------------------

def build_shipment(world: World, order: dict, ship_dt: datetime, deliver_dt: datetime | None,
                   promised_date, ordered: bool) -> dict:
    rng = world.rng
    wh_id = world.order_warehouse[order["sales_order_id"]]
    if deliver_dt is not None:
        status = "delivered"
    elif ordered:
        status = "in_transit"
    else:
        status = "picking"
    shipment_id = world.next_id("shipments")
    return {
        "shipment_id": shipment_id,
        "shipment_number": f"SHP-{shipment_id:07d}",
        "sales_order_id": order["sales_order_id"],
        "warehouse_id": wh_id,
        "carrier": rng.choice(CARRIERS),
        "tracking_number": f"TRK{order['sales_order_id']:07d}{rng.randint(100, 999)}",
        "shipped_date": ship_dt,
        "promised_date": promised_date,
        "delivered_date": deliver_dt,
        "status": status,
        "shipping_cost": money(Decimal(rng.randint(3, 25))),
        "created_at": ship_dt,
        "updated_at": deliver_dt or ship_dt,
    }


def build_payment(world: World, order: dict, pay_dt: datetime, completed: bool) -> dict:
    rng = world.rng
    method = rng.choice(PAY_METHODS)
    payment_id = world.next_id("payments")
    return {
        "payment_id": payment_id,
        "payment_number": f"PAY-{payment_id:07d}",
        "sales_order_id": order["sales_order_id"],
        "payment_date": pay_dt,
        "amount": order["total_amount"] if order["total_amount"] > 0 else money(Decimal("0.01")),
        "method": method,
        "status": "completed" if completed else "pending",
        "reference": f"REF{rng.randint(10**6, 10**7 - 1)}",
        "created_at": pay_dt,
    }


def build_return(world: World, order: dict, return_dt: datetime,
                 items: list[dict]) -> tuple[dict, list[dict]]:
    rng = world.rng
    return_id = world.next_id("returns")
    refund_total = sum((it["line_refund"] for it in items), Decimal("0"))
    ret = {
        "return_id": return_id,
        "return_number": f"RET-{return_id:07d}",
        "sales_order_id": order["sales_order_id"],
        "customer_id": order["customer_id"],
        "warehouse_id": world.order_warehouse[order["sales_order_id"]],
        "return_date": return_dt,
        "reason": rng.choice(RETURN_REASONS),
        "status": "received",
        "refund_amount": refund_total,
        "created_at": return_dt,
        "updated_at": return_dt,
    }
    return_items = []
    for it in items:
        return_items.append({
            "return_item_id": world.next_id("return_items"),
            "return_id": return_id,
            "sales_order_item_id": it["sales_order_item_id"],
            "product_id": it["product_id"],
            "quantity": it["quantity"],
            "unit_price": it["unit_price"],
            "line_refund": it["line_refund"],
            "item_condition": it["item_condition"],
            "disposition": it["disposition"],
            "created_at": return_dt,
        })
    return ret, return_items

"""Procurement generators: purchase orders, line items, goods receipts.

Purchase orders are planned from each product's expected demand at each
warehouse. The total ordered quantity is solved to track the aggregate demand
(plus a buffer) rather than being hardcoded, and receipt dates incorporate
supplier lead times and on-time performance so delays and partial receipts
appear naturally.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .base import World, money, rand_dt


def _month_datetime(rng, year: int, month: int) -> datetime:
    if month == 12:
        last = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    else:
        last = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return rand_dt(rng, first, last)


def _solve_scale(lines: list[dict], target: float) -> float:
    """Find a scale factor so that sum(max(min_qty, weight*scale)) ~= target."""
    def total(scale: float) -> float:
        return sum(max(ln["min_qty"], round(ln["weight"] * scale)) for ln in lines)

    lo, hi = 0.0, 10000.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if total(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def generate(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    tax_rate = cfg.default_tax_rate
    year = cfg.history_start.year

    by_supplier: dict[int, dict[int, dict]] = defaultdict(dict)
    for sp in world.supplier_products:
        by_supplier[sp["supplier_id"]][sp["product_id"]] = sp

    world.purchase_receipts: list[dict] = []

    # choose PO warehouses in proportion to each warehouse's demand
    wh_demand: dict[int, int] = defaultdict(int)
    for (wh_id, _pid), qty in world.demand_by_wh_prod.items():
        wh_demand[wh_id] += qty
    wh_weights = [max(wh_demand.get(w["warehouse_id"], 0), 1) for w in world.warehouses]

    def _weighted_products(wh_id, candidates, k):
        pool = [(pid, max(world.demand_by_wh_prod.get((wh_id, pid), 0), 0.2))
                for pid in candidates]
        k = min(k, len(pool))
        chosen = []
        for _ in range(k):
            total = sum(w for _, w in pool)
            r = rng.random() * total
            acc = 0.0
            for idx, (pid, w) in enumerate(pool):
                acc += w
                if acc >= r:
                    chosen.append(pid)
                    pool.pop(idx)
                    break
            else:
                chosen.append(pool.pop()[0])
        return chosen

    # ---- phase A: plan POs and their product lines -----------------------
    plans: list[dict] = []
    all_lines: list[dict] = []
    for _ in range(cfg.n_purchase_orders):
        wh = rng.choices(world.warehouses, weights=wh_weights, k=1)[0]
        wh_id = wh["warehouse_id"]
        supplier = rng.choice(world.suppliers)
        sup_id = supplier["supplier_id"]
        supplied = by_supplier[sup_id]
        candidates = [pid for pid in supplied if world.assortment.get((wh_id, pid), False)]
        if not candidates:
            continue
        product_ids = _weighted_products(wh_id, candidates, rng.randint(3, 30))
        # order ~1 month before the month where demand is expected
        monthly = world.demand_by_wh_month.get(wh_id, [1] * 12)
        demand_month = rng.choices(range(1, 13), weights=[max(x, 1) for x in monthly], k=1)[0]
        order_month = max(1, demand_month - 1)
        order_dt = _month_datetime(rng, year, order_month)
        lead = supplier["default_lead_time_days"]

        lines = []
        for pid in product_ids:
            sp = supplied[pid]
            ln = {
                "product_id": pid,
                "sp": sp,
                "base": world.product_demand[pid]["base_daily"],
                "min_qty": sp["min_order_qty"],
                "qty": 0,
            }
            lines.append(ln)
            all_lines.append(ln)
        plans.append({
            "wh_id": wh_id, "supplier": supplier, "sup_id": sup_id,
            "order_dt": order_dt, "expected": (order_dt + timedelta(days=lead)).date(),
            "lines": lines,
        })

    # ---- coverage: every demanded (warehouse, product) is replenished ----
    wh_to_plans: dict[int, list[dict]] = defaultdict(list)
    for plan in plans:
        wh_to_plans[plan["wh_id"]].append(plan)
    line_count: dict[tuple[int, int], int] = defaultdict(int)
    for plan in plans:
        for ln in plan["lines"]:
            line_count[(plan["wh_id"], ln["product_id"])] += 1

    for (wh_id, pid), dem in world.demand_by_wh_prod.items():
        if dem <= 0 or not world.assortment.get((wh_id, pid), False):
            continue
        for _ in range(max(0, 2 - line_count[(wh_id, pid)])):
            target_plan = next((pl for pl in wh_to_plans.get(wh_id, [])
                                if pid in by_supplier[pl["sup_id"]]), None)
            if target_plan is None:
                break
            sp = by_supplier[target_plan["sup_id"]][pid]
            ln = {"product_id": pid, "sp": sp,
                  "base": world.product_demand[pid]["base_daily"],
                  "min_qty": sp["min_order_qty"], "qty": 0}
            target_plan["lines"].append(ln)
            all_lines.append(ln)
            line_count[(wh_id, pid)] += 1

    total_demand = sum(world.demand_by_wh_prod.values())
    target = max(total_demand * 1.02, 1.0)

    # weight each line by the demand it should satisfy, split across the lines
    # that replenish the same (warehouse, product)
    for plan in plans:
        for ln in plan["lines"]:
            pair = (plan["wh_id"], ln["product_id"])
            dem = world.demand_by_wh_prod.get(pair, 0)
            cnt = max(line_count[pair], 1)
            ln["weight"] = (dem / cnt) if dem > 0 else max(ln["base"] * 0.1, 0.5)

    scale = _solve_scale(all_lines, target)
    for ln in all_lines:
        ln["qty"] = max(ln["min_qty"], round(ln["weight"] * scale))

    # ---- phase B: materialise rows, receipts and statuses ---------------
    for plan in plans:
        po_id = world.next_id("purchase_orders")
        order_dt = plan["order_dt"]
        wh_id = plan["wh_id"]
        sup_id = plan["sup_id"]
        supplier = plan["supplier"]
        po = {
            "purchase_order_id": po_id,
            "po_number": f"PO-{order_dt.year}{order_dt.month:02d}-{po_id:06d}",
            "supplier_id": sup_id,
            "warehouse_id": wh_id,
            "requested_by": rng.choice(
                [e for e in world.employees if e["department"] == "Procurement"]
                or world.employees
            )["employee_id"],
            "approved_by": None,
            "status": "ordered",
            "order_date": order_dt,
            "expected_date": plan["expected"],
            "subtotal": Decimal("0"),
            "tax_amount": Decimal("0"),
            "shipping_amount": Decimal("0"),
            "total_amount": Decimal("0"),
            "currency": cfg.currency,
            "notes": None,
            "created_at": order_dt,
            "updated_at": order_dt,
        }

        subtotal = Decimal("0")
        lines = []
        for ln_num, ln in enumerate(plan["lines"], start=1):
            sp = ln["sp"]
            qty = ln["qty"]
            unit_cost = sp["unit_cost"]
            line_total = money(unit_cost * qty)
            po_item_id = world.next_id("purchase_order_items")
            line = {
                "po_item_id": po_item_id,
                "purchase_order_id": po_id,
                "line_number": ln_num,
                "product_id": ln["product_id"],
                "quantity_ordered": qty,
                "quantity_received": 0,
                "unit_cost": unit_cost,
                "line_total": line_total,
                "created_at": order_dt,
            }
            world.add("purchase_order_items", line)
            world.po_items.append(line)
            lines.append(line)
            subtotal += line_total

        shipping = money(Decimal(rng.choice([0, 25, 50, 75, 100, 150])))
        tax = money(subtotal * tax_rate)
        po["subtotal"] = subtotal
        po["shipping_amount"] = shipping
        po["tax_amount"] = tax
        po["total_amount"] = subtotal + tax + shipping

        perf = world.supplier_perf[sup_id]
        r = rng.random()
        if r < 0.06:
            po["status"] = "pending_approval"
        elif r < 0.09:
            po["status"] = "cancelled"
        else:
            approvers = [e for e in world.employees
                         if e["department"] in ("Management", "Finance")
                         and e["employee_id"] != po["requested_by"]]
            if not approvers:
                approvers = [e for e in world.employees
                             if e["employee_id"] != po["requested_by"]] or world.employees
            po["approved_by"] = rng.choice(approvers)["employee_id"]
            delayed = rng.random() > perf["on_time_rate"]
            delay_days = rng.randint(1, 21) if delayed else rng.randint(-2, 1)
            receipt_dt = datetime.combine(plan["expected"] + timedelta(days=delay_days),
                                          time(hour=rng.randint(7, 15)),
                                          tzinfo=timezone.utc)
            receipt_dt = max(receipt_dt, order_dt + timedelta(days=1))

            if receipt_dt > datetime.combine(cfg.history_end, time.max, tzinfo=timezone.utc):
                po["status"] = "ordered"
            else:
                partial = rng.random() < cfg.partial_po_receipt_rate
                receipt_id = world.next_id("goods_receipts")
                gr_items = []
                received_total = 0
                ordered_total = 0
                for line, ln in zip(lines, plan["lines"]):
                    ordered_total += line["quantity_ordered"]
                    if partial:
                        qrecv = max(1, int(round(line["quantity_ordered"] * rng.uniform(0.4, 0.9))))
                        qrecv = min(qrecv, line["quantity_ordered"])
                    else:
                        qrecv = line["quantity_ordered"]
                    line["quantity_received"] = qrecv
                    received_total += qrecv
                    gr_item = {
                        "receipt_item_id": world.next_id("goods_receipt_items"),
                        "receipt_id": receipt_id,
                        "po_item_id": line["po_item_id"],
                        "product_id": line["product_id"],
                        "quantity_received": qrecv,
                        "unit_cost": line["unit_cost"],
                        "line_total": money(line["unit_cost"] * qrecv),
                        "created_at": receipt_dt,
                    }
                    world.add("goods_receipt_items", gr_item)
                    gr_items.append(gr_item)

                complete = received_total >= ordered_total
                po["status"] = "received" if complete else "partially_received"
                gr = {
                    "receipt_id": receipt_id,
                    "receipt_number": f"GR-{receipt_dt.year}{receipt_dt.month:02d}-{receipt_id:06d}",
                    "purchase_order_id": po_id,
                    "warehouse_id": wh_id,
                    "received_by": rng.choice(
                        [e for e in world.employees
                         if e["department"] == "Warehouse" and e["warehouse_id"] == wh_id]
                        or [e for e in world.employees
                            if e["department"] == "Warehouse"]
                        or world.employees
                    )["employee_id"],
                    "received_date": receipt_dt,
                    "status": "complete" if complete else "partial",
                    "notes": "partial delivery" if not complete else None,
                    "created_at": receipt_dt,
                }
                world.add("goods_receipts", gr)
                world.purchase_receipts.append({
                    "receipt_id": receipt_id,
                    "po_id": po_id,
                    "warehouse_id": wh_id,
                    "date": receipt_dt,
                    "items": gr_items,
                })
        po["updated_at"] = po["order_date"]
        world.add("purchase_orders", po)
        world.purchase_orders.append(po)

    world.rows["purchase_orders"].sort(key=lambda r: r["purchase_order_id"])
    world.rows["purchase_order_items"].sort(key=lambda r: r["po_item_id"])

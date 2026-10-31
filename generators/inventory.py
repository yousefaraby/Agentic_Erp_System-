"""Inventory generators and the event-driven inventory ledger.

This module owns the inventory accounting rules:

* Every (warehouse, product) balance equals the sum of that pair's stock
  movements (``initial`` seeded at the start of the window).
* Sales consume stock at dispatch time; unfulfilled quantity is left as a
  backorder (order status ``pending``/``reserved``).
* A completed stock transfer removes stock from the source and adds it to the
  destination; small in-transit losses are modelled as a difference between
  quantity_shipped and quantity_received.
* Customer returns with disposition ``restock`` add stock back.

The ledger is a single chronological simulation with a priority queue, so all
historical timestamps and balances remain consistent.
"""
from __future__ import annotations

import heapq
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .base import World, money

STATUS_PRIORITY = {
    "purchase_receipt": 1,
    "transfer_dispatch": 2,
    "transfer_receive": 3,
    "sale": 4,
    "return": 5,
}


def init_assortment(world: World) -> None:
    """Decide which products each warehouse carries and store the mapping."""
    rng = world.rng
    world.assortment: dict[tuple[int, int], bool] = {}
    world.warehouse_products: dict[int, list[int]] = {}
    for wh in world.warehouses:
        stocked: list[int] = []
        is_central = wh["warehouse_type"] == "central"
        p_stock = 0.99 if is_central else 0.9
        for p in world.products:
            if rng.random() < p_stock:
                world.assortment[(wh["warehouse_id"], p["product_id"])] = True
                stocked.append(p["product_id"])
            else:
                world.assortment[(wh["warehouse_id"], p["product_id"])] = False
        world.warehouse_products[wh["warehouse_id"]] = stocked


def generate_transfers(world: World) -> None:
    """Create stock transfer requests with a realistic status mixture."""
    from .base import rand_dt
    cfg = world.cfg
    rng = world.rng
    start = datetime.combine(cfg.history_start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(cfg.history_end, time(23, 59, 59), tzinfo=timezone.utc)

    for _ in range(cfg.n_stock_transfers):
        if rng.random() < 0.75:
            src = world.central_warehouse
        else:
            src = rng.choice(world.warehouses)
        candidates = [w for w in world.warehouses if w["warehouse_id"] != src["warehouse_id"]]
        dst = rng.choice(candidates)
        pool = [(pid, world.product_demand[pid]["base_daily"])
                for pid in world.warehouse_products[src["warehouse_id"]]]
        products = []
        for _ in range(min(rng.randint(1, 3), len(pool))):
            total_w = sum(w for _, w in pool) or 1.0
            r = rng.random() * total_w
            acc = 0.0
            for idx, (pid, w) in enumerate(pool):
                acc += w
                if acc >= r:
                    products.append(pid)
                    pool.pop(idx)
                    break
            else:
                products.append(pool.pop()[0])
        request_dt = rand_dt(rng, start, end)
        status = rng.choices(
            ["completed", "pending", "rejected", "cancelled", "approved"],
            weights=[72, 12, 6, 6, 4], k=1,
        )[0]
        tr_id = world.next_id("stock_transfers")
        items = []
        total_qty = 0
        for pid in products:
            qty = rng.randint(1, 8)
            total_qty += qty
            item = {
                "transfer_item_id": world.next_id("stock_transfer_items"),
                "transfer_id": tr_id,
                "product_id": pid,
                "quantity_requested": qty,
                "quantity_shipped": 0,
                "quantity_received": 0,
                "unit_cost": world.product_by_id[pid]["cost_price"],
                "created_at": request_dt,
            }
            world.add("stock_transfer_items", item)
            items.append(item)

        dispatched = request_dt + timedelta(days=rng.randint(1, 4))
        received = dispatched + timedelta(days=rng.randint(2, 7))
        requesters = [e for e in world.employees
                      if e["department"] == "Warehouse"
                      and world.branch_warehouse[e["branch_id"]]["warehouse_id"] == src["warehouse_id"]]
        requesters = requesters or [e for e in world.employees if e["department"] == "Warehouse"] or world.employees
        requested_by = rng.choice(requesters)["employee_id"]
        approvers = [e for e in world.employees
                     if e["department"] in ("Management", "Warehouse")
                     and world.branch_warehouse[e["branch_id"]]["warehouse_id"] == src["warehouse_id"]
                     and e["employee_id"] != requested_by]
        approvers = approvers or [e for e in world.employees
                                  if e["department"] in ("Management", "Warehouse")
                                  and e["employee_id"] != requested_by]
        approvers = approvers or [e for e in world.employees if e["employee_id"] != requested_by] or world.employees
        transfer = {
            "transfer_id": tr_id,
            "transfer_number": f"TRF-{tr_id:06d}",
            "source_warehouse_id": src["warehouse_id"],
            "destination_warehouse_id": dst["warehouse_id"],
            "requested_by": requested_by,
            "approved_by": (rng.choice(approvers)["employee_id"]
                            if status in ("completed", "approved") else None),
            "request_date": request_dt,
            "status": status,
            "dispatched_date": None,
            "received_date": None,
            "expected_date": (request_dt + timedelta(days=rng.randint(3, 10))).date(),
            "total_quantity": total_qty,
            "reason": rng.choice(["replenishment", "rebalance", "stockout_relief",
                                  "seasonal", "consolidation"]),
            "notes": None,
            "created_at": request_dt,
            "updated_at": request_dt,
        }
        record = {**transfer, "_items": items,
                  "_dispatch_at": dispatched, "_receive_at": received}
        world.rows["stock_transfers"].append(record)
        world.transfers.append(record)


def run_ledger(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    start = datetime.combine(cfg.history_start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(cfg.history_end, time(23, 59, 59), tzinfo=timezone.utc)

    balance: dict[tuple[int, int], int] = {}
    priority_queue: list = []
    seq = 0

    def push(ts, kind, payload):
        nonlocal seq
        seq += 1
        heapq.heappush(priority_queue, (ts, STATUS_PRIORITY[kind], seq, kind, payload))

    def add_movement(wh_id, pid, mtype, change, ts, ref_type, ref_id, unit_cost=None, notes=None):
        key = (wh_id, pid)
        new_balance = balance.get(key, 0) + change
        assert new_balance >= 0, f"negative balance {new_balance} at {key}"
        balance[key] = new_balance
        world.add("stock_movements", {
            "movement_id": world.next_id("stock_movements"),
            "warehouse_id": wh_id,
            "product_id": pid,
            "movement_type": mtype,
            "quantity_change": change,
            "balance_after": new_balance,
            "reference_type": ref_type,
            "reference_id": ref_id,
            "unit_cost": unit_cost,
            "movement_date": ts,
            "notes": notes,
            "created_at": ts,
        })

    # ---- 1. seed initial stock ------------------------------------------
    world.initial_stock: dict[tuple[int, int], int] = {}
    seed_dt = start - timedelta(days=1)
    for wh in world.warehouses:
        wh_id = wh["warehouse_id"]
        central = wh["warehouse_type"] == "central"
        for pid in world.warehouse_products[wh_id]:
            base = world.product_demand[pid]["base_daily"]
            pair_demand = world.demand_by_wh_prod.get((wh_id, pid), 0)
            if pair_demand > 0:
                # Maintain service level while leaving realistic shortage scenarios.
                lo, hi = (0.32, 0.44) if not central else (0.36, 0.50)
                qty = int(round(pair_demand * rng.uniform(lo, hi)))
            else:
                qty = int(round(base * rng.uniform(2, 6)))
            # deliberate shortage pairs -> stockout scenarios
            if rng.random() < 0.03:
                qty = 0
            world.initial_stock[(wh_id, pid)] = qty
            if qty > 0:
                add_movement(wh_id, pid, "initial", qty, seed_dt, "manual",
                             None, world.product_by_id[pid]["cost_price"],
                             "opening balance")

    # ---- 2. queue purchase receipts -------------------------------------
    for rec in world.purchase_receipts:
        push(rec["date"], "purchase_receipt", rec)

    # ---- 3. queue transfers that actually move stock --------------------
    for tr in world.transfers:
        if tr["status"] == "completed":
            if tr["_receive_at"] <= end:
                push(tr["_dispatch_at"], "transfer_dispatch", tr)
            else:
                # not dispatched within the window
                tr["status"] = "pending"

    # ---- 4. queue sales orders ------------------------------------------
    order_by_id = {o["sales_order_id"]: o for o in world.sales_orders}
    items_by_order: dict[int, list[dict]] = {}
    for it in world.rows["sales_order_items"]:
        items_by_order.setdefault(it["sales_order_id"], []).append(it)

    for order in world.sales_orders:
        if order["status"] == "cancelled":
            continue
        ship_dt = order["order_date"] + timedelta(days=rng.randint(1, 5),
                                                  hours=rng.randint(0, 8))
        if ship_dt > end:
            order["status"] = "reserved"
            continue
        push(ship_dt, "sale", (order, ship_dt))

    # ---- 5. process events ----------------------------------------------
    world.order_outcome: dict[int, dict] = {}
    world.payment_by_order: dict[int, dict] = {}

    def schedule_return(order, deliver_dt):
        if deliver_dt is None or deliver_dt >= end:
            return
        if rng.random() >= cfg.return_rate:
            return
        return_dt = deliver_dt + timedelta(days=rng.randint(3, 30),
                                           hours=rng.randint(0, 12))
        if return_dt >= end:
            return
        push(return_dt, "return", (order, return_dt))

    while priority_queue:
        ts, _, _, kind, payload = heapq.heappop(priority_queue)

        if kind == "purchase_receipt":
            rec = payload
            for item in rec["items"]:
                add_movement(rec["warehouse_id"], item["product_id"],
                             "purchase_receipt", item["quantity_received"], ts,
                             "goods_receipt", rec["receipt_id"], item["unit_cost"])

        elif kind == "transfer_dispatch":
            tr = payload
            src = tr["source_warehouse_id"]
            dst = tr["destination_warehouse_id"]

            def feasible_from(w_id):
                return all(balance.get((w_id, it["product_id"]), 0) >= it["quantity_requested"]
                           for it in tr["_items"])

            if not feasible_from(src):
                alternatives = [w["warehouse_id"] for w in world.warehouses
                                if w["warehouse_id"] not in (src, dst)
                                and feasible_from(w["warehouse_id"])]
                if alternatives:
                    src = max(alternatives, key=lambda w_id: min(
                        balance.get((w_id, it["product_id"]), 0) - it["quantity_requested"]
                        for it in tr["_items"]))
                    tr["source_warehouse_id"] = src
                else:
                    tr["status"] = "rejected"
                    tr["updated_at"] = ts
                    continue
            for it in tr["_items"]:
                add_movement(src, it["product_id"],
                             "transfer_out", -it["quantity_requested"], ts,
                             "stock_transfer", tr["transfer_id"], it["unit_cost"])
                it["quantity_shipped"] = it["quantity_requested"]
            tr["status"] = "in_transit"
            tr["dispatched_date"] = ts
            tr["updated_at"] = ts
            push(tr["_receive_at"], "transfer_receive", tr)

        elif kind == "transfer_receive":
            tr = payload
            loss_transfer = rng.random() < 0.05
            loss_item = rng.choice(tr["_items"]) if loss_transfer else None
            for it in tr["_items"]:
                qty = it["quantity_shipped"]
                if loss_item is it and qty > 1:
                    qty -= 1
                it["quantity_received"] = qty
                add_movement(tr["destination_warehouse_id"], it["product_id"],
                             "transfer_in", qty, ts, "stock_transfer",
                             tr["transfer_id"], it["unit_cost"],
                             "in-transit loss" if (loss_item is it) else None)
            tr["status"] = "completed"
            tr["received_date"] = ts
            tr["updated_at"] = ts

        elif kind == "sale":
            order, ship_dt = payload
            wh_id = world.order_warehouse[order["sales_order_id"]]
            order_items = items_by_order.get(order["sales_order_id"], [])
            fulfilled_any = False
            fulfilled_all = len(order_items) > 0
            for it in order_items:
                avail = balance.get((wh_id, it["product_id"]), 0)
                take = min(it["quantity"], avail)
                it["quantity_fulfilled"] = take
                if take > 0:
                    add_movement(wh_id, it["product_id"], "sale", -take, ship_dt,
                                 "sales_order", order["sales_order_id"],
                                 world.product_by_id[it["product_id"]]["cost_price"])
                    fulfilled_any = True
                if take < it["quantity"]:
                    fulfilled_all = False

            deliver_dt = None
            promised_transit = rng.randint(2, 5)
            promised_date = (ship_dt + timedelta(days=promised_transit)).date()
            if fulfilled_all:
                if rng.random() < cfg.delayed_shipment_rate:
                    actual_transit = promised_transit + rng.randint(1, 10)
                else:
                    actual_transit = rng.randint(1, promised_transit)
                cand = ship_dt + timedelta(days=actual_transit)
                deliver_dt = cand if cand <= end else None
                order["status"] = "delivered" if deliver_dt else "dispatched"
            elif fulfilled_any:
                order["status"] = "dispatched"
            else:
                order["status"] = "pending"
            order["updated_at"] = ship_dt

            if fulfilled_any:
                shipment = _build_shipment(world, order, ship_dt, deliver_dt,
                                           promised_date, True)
                world.add("shipments", shipment)
                completed = order["status"] == "delivered"
                payment = _build_payment(world, order, ship_dt, completed)
                world.add("payments", payment)
                world.payment_by_order[order["sales_order_id"]] = payment
                if deliver_dt is not None:
                    schedule_return(order, deliver_dt)
            elif order["status"] == "pending":
                payment = _build_payment(world, order, ship_dt, False)
                world.add("payments", payment)
                world.payment_by_order[order["sales_order_id"]] = payment

            world.order_outcome[order["sales_order_id"]] = {
                "status": order["status"],
                "warehouse_id": wh_id,
                "ship_dt": ship_dt,
                "deliver_dt": deliver_dt,
            }

        elif kind == "return":
            order, return_dt = payload
            wh_id = world.order_warehouse[order["sales_order_id"]]
            order_items = [it for it in items_by_order.get(order["sales_order_id"], [])
                           if it["quantity_fulfilled"] > 0]
            if not order_items:
                continue
            n = rng.randint(1, len(order_items))
            chosen = rng.sample(order_items, n)
            return_id = world.next_id("returns")
            ret_items = []
            for it in chosen:
                qty = rng.randint(1, it["quantity_fulfilled"])
                disp = rng.choices(
                    ["restock", "scrap", "repair", "return_to_supplier"],
                    weights=[75, 15, 5, 5], k=1,
                )[0]
                cond = rng.choice(["new", "opened", "damaged", "used"])
                returned_subtotal = money(it["unit_price"] * qty)
                returned_discount = money(returned_subtotal * it["discount_rate"])
                returned_tax = money((returned_subtotal - returned_discount) * it["tax_rate"])
                line_refund = returned_subtotal - returned_discount + returned_tax
                ret_items.append({
                    "sales_order_item_id": it["sales_order_item_id"],
                    "product_id": it["product_id"],
                    "quantity": qty,
                    "unit_price": it["unit_price"],
                    "line_refund": line_refund,
                    "item_condition": cond,
                    "disposition": disp,
                })
                if disp == "restock":
                    add_movement(wh_id, it["product_id"], "customer_return", qty,
                                 return_dt, "return", return_id,
                                 world.product_by_id[it["product_id"]]["cost_price"],
                                 "customer return restocked")

            refund_total = sum((x["line_refund"] for x in ret_items), Decimal("0"))
            ret = {
                "return_id": return_id,
                "return_number": f"RET-{return_id:07d}",
                "sales_order_id": order["sales_order_id"],
                "customer_id": order["customer_id"],
                "warehouse_id": wh_id,
                "return_date": return_dt,
                "reason": rng.choice(["defective", "wrong_item", "not_as_described",
                                      "changed_mind", "damaged_in_transit",
                                      "late_delivery", "other"]),
                "status": "refunded",
                "refund_amount": refund_total,
                "created_at": return_dt,
                "updated_at": return_dt,
            }
            world.add("returns", ret)
            payment = world.payment_by_order.get(order["sales_order_id"])
            if payment is None:
                raise ValueError(f"return {return_id} has no payment for its delivered order")
            refund_id = world.next_id("payment_refunds")
            world.add("payment_refunds", {
                "refund_id": refund_id,
                "refund_number": f"RFD-{refund_id:07d}",
                "payment_id": payment["payment_id"],
                "return_id": return_id,
                "refund_date": return_dt,
                "amount": refund_total,
                "status": "completed",
                "created_at": return_dt,
            })
            for x in ret_items:
                world.add("return_items", {
                    "return_item_id": world.next_id("return_items"),
                    "return_id": return_id,
                    "sales_order_item_id": x["sales_order_item_id"],
                    "product_id": x["product_id"],
                    "quantity": x["quantity"],
                    "unit_price": x["unit_price"],
                    "line_refund": x["line_refund"],
                    "item_condition": x["item_condition"],
                    "disposition": x["disposition"],
                    "created_at": return_dt,
                })

    # ---- 6. finalise inventory table ------------------------------------
    for wh in world.warehouses:
        wh_id = wh["warehouse_id"]
        for pid in world.warehouse_products[wh_id]:
            qty = balance.get((wh_id, pid), 0)
            world.add("inventory", {
                "inventory_id": world.next_id("inventory"),
                "warehouse_id": wh_id,
                "product_id": pid,
                "quantity_on_hand": qty,
                "quantity_reserved": 0,
                "reorder_point": world.product_by_id[pid]["reorder_point"],
                "last_counted_at": cfg.history_end,
                "updated_at": end,
            })

    # Reserve available stock for orders that remain open at the snapshot.
    inventory_by_pair = {(r["warehouse_id"], r["product_id"]): r
                         for r in world.rows["inventory"]}
    for order in sorted((o for o in world.sales_orders if o["status"] == "reserved"),
                        key=lambda o: (o["order_date"], o["sales_order_id"])):
        wh_id = world.order_warehouse[order["sales_order_id"]]
        lines = items_by_order.get(order["sales_order_id"], [])
        can_reserve = all(
            (wh_id, line["product_id"]) in inventory_by_pair
            and int(inventory_by_pair[(wh_id, line["product_id"])]["quantity_on_hand"])
                - int(inventory_by_pair[(wh_id, line["product_id"])]["quantity_reserved"])
                >= line["quantity"]
            for line in lines
        )
        if not can_reserve or not lines:
            order["status"] = "pending"
            order["updated_at"] = end
            continue
        reservation_id = world.next_id("inventory_reservations")
        world.add("inventory_reservations", {
            "reservation_id": reservation_id,
            "sales_order_id": order["sales_order_id"],
            "warehouse_id": wh_id,
            "status": "active",
            "reserved_at": end,
            "expires_at": end + timedelta(days=7),
            "created_at": end,
            "updated_at": end,
        })
        for line in lines:
            world.add("inventory_reservation_items", {
                "reservation_item_id": world.next_id("inventory_reservation_items"),
                "reservation_id": reservation_id,
                "sales_order_item_id": line["sales_order_item_id"],
                "warehouse_id": wh_id,
                "product_id": line["product_id"],
                "quantity": line["quantity"],
                "created_at": end,
            })
            inventory_by_pair[(wh_id, line["product_id"])]["quantity_reserved"] += line["quantity"]


def _build_shipment(world, order, ship_dt, deliver_dt, promised_date, ordered):
    from .sales import build_shipment
    return build_shipment(world, order, ship_dt, deliver_dt, promised_date, ordered)


def _build_payment(world, order, pay_dt, completed):
    from .sales import build_payment
    return build_payment(world, order, pay_dt, completed)

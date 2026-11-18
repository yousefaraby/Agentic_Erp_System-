"""Inventory agent: reserve stock, plan transfers, and post receipts."""

from collections import defaultdict

from storage import Assessment, Blocked, Draft, enqueue, find_order, log, order_lines, save_outbound, source
from .analytics import low_stock_rows


def stock_review_agent(db, event):
    rows = low_stock_rows(db)
    balances = ", ".join(
        f"{row['warehouse']}/{row['sku']} {row['available']} {row['unit']} available"
        for row in rows
    )
    summary = f"{len(rows)} warehouse stock balances at or below their reorder point."
    return Assessment(f"{summary} {balances}" if balances else summary,
                      tuple(startup_stock_plan(db, event["as_of"])))


def stock_replenishment(db, row, day):
    from .procurement import supplier_quotes

    destination, product = row["warehouse_id"], row["product_id"]
    purchase_orders = {r["purchase_order_id"] for r in source(db, "purchase_orders")
                       if r["warehouse_id"] == destination and r["status"] in {"ordered", "partially_received"}}
    incoming = sum(max(0, r["quantity_ordered"] - r["quantity_received"])
                   for r in source(db, "purchase_order_items")
                   if r["purchase_order_id"] in purchase_orders and r["product_id"] == product)
    transfers = {r["transfer_id"] for r in source(db, "stock_transfers")
                 if r["destination_warehouse_id"] == destination and r["status"] == "in_transit"}
    incoming += sum(max(0, r["quantity_shipped"] - r["quantity_received"])
                    for r in source(db, "stock_transfer_items")
                    if r["transfer_id"] in transfers and r["product_id"] == product)
    incoming += db.execute("""
        SELECT coalesce(sum(quantity), 0) FROM demo_replenishments
        WHERE destination_id = ? AND product_id = ? AND status <> 'received'
    """, (destination, product)).fetchone()[0]
    if db.execute("""
        SELECT 1 FROM stock_replenishments WHERE destination_id = ? AND product_id = ? AND status = 'inbound'
    """, (destination, product)).fetchone():
        return None
    needed = row["reorder_point"] - row["available"] - incoming
    if needed < 0 or (incoming and needed == 0):
        return None
    needed = max(1, needed)
    target = f"{row['warehouse']}/{row['sku']}"
    payload = {**row, "as_of": day, "quantity": needed, "incoming": incoming}
    eligible = {r["warehouse_id"] for r in source(db, "branch_warehouse_services") if r["can_transfer"]}
    points = {r["warehouse_id"]: r["reorder_point"] for r in source(db, "inventory") if r["product_id"] == product}
    donors = db.execute("""
        SELECT w.id, w.code, i.on_hand - i.reserved AS available FROM inventory i
        JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.product_id = ? AND w.id <> ? ORDER BY available DESC, w.id
    """, (product, destination))
    for donor in donors:
        if (destination in eligible and donor["id"] in eligible
                and donor["available"] - points.get(donor["id"], 0) >= needed):
            return Draft("replenish_stock", target,
                         f"Transfer {needed} {row['unit']} of {row['sku']} from {donor['code']} to {row['warehouse']}.",
                         {**payload, "method": "transfer", "origin_id": donor["id"], "origin": donor["code"]})
    quote = supplier_quotes(db, day, product, needed)
    if quote:
        return Draft("replenish_stock", target,
                     f"Buy {quote['quantity']} {row['unit']} of {row['sku']} for {row['warehouse']} "
                     f"from {quote['supplier']} for USD {quote['total_minor'] / 100:,.2f}.",
                     {**payload, **quote, "method": "purchase"})
    return None


def startup_stock_plan(db, day):
    return [draft for row in low_stock_rows(db) if (draft := stock_replenishment(db, row, day))]


def replenish_stock(db, approval, payload):
    current = next((row for row in low_stock_rows(db)
                    if (row["warehouse_id"], row["product_id"]) ==
                    (payload["warehouse_id"], payload["product_id"])), None)
    draft = stock_replenishment(db, current, payload["as_of"]) if current else None
    if not draft or draft.payload != payload:
        raise Blocked("Stock, inbound supply, or the available quote changed since this proposal.")
    quantity = payload["quantity"]
    if payload["method"] == "transfer":
        db.execute("UPDATE inventory SET on_hand = on_hand - ? WHERE warehouse_id = ? AND product_id = ?",
                   (quantity, payload["origin_id"], payload["product_id"]))
        channel, recipient = "warehouse_demo", payload["origin"]
    else:
        channel, recipient = "email_demo", payload["recipient"]
    db.execute("INSERT INTO stock_replenishments VALUES (?, ?, ?, ?, 'inbound')",
               (approval["id"], payload["warehouse_id"], payload["product_id"], quantity))
    save_outbound(db, approval["id"], channel, recipient, draft.summary)
    enqueue(db, f"stock-receipt:{approval['id']}", "demo.stock_receipt", {"replenishment_id": approval["id"]})
    return {"status": "inbound", "quantity": quantity, "delivery": "simulated"}


def stock_receipt_agent(db, event):
    row = db.execute("""
        SELECT s.*, p.sku, w.code FROM stock_replenishments s JOIN products p ON p.id = s.product_id
        JOIN warehouses w ON w.id = s.destination_id WHERE s.id = ? AND s.status = 'inbound'
    """, (event["replenishment_id"],)).fetchone()
    if not row:
        return Assessment("No stock replenishment is awaiting receipt.")
    summary = f"Record receipt of {row['quantity']} {row['sku']} at {row['code']}."
    return Assessment(summary, (Draft("post_stock_receipt", str(row["id"]), summary, dict(row)),))


def post_stock_receipt(db, approval, payload):
    drafts = stock_receipt_agent(db, {"replenishment_id": payload["id"]}).drafts
    if not drafts or drafts[0].payload != payload:
        raise Blocked("The replenishment is no longer awaiting receipt.")
    db.execute("UPDATE inventory SET on_hand = on_hand + ? WHERE warehouse_id = ? AND product_id = ?",
               (payload["quantity"], payload["destination_id"], payload["product_id"]))
    db.execute("UPDATE stock_replenishments SET status = 'received' WHERE id = ?", (payload["id"],))
    return {"receipt_status": "received", "orders_reserved": reserve_waiting_orders(db, approval["event_key"])}


def check_stock(db, branch_id, lines):
    allocations, shortages = {}, []
    for product, requested in lines:
        remaining = requested
        warehouses = db.execute("""
            SELECT w.id, w.code, i.on_hand - i.reserved AS available
            FROM branch_warehouses AS bw
            JOIN warehouses AS w ON w.id = bw.warehouse_id
            JOIN inventory AS i ON i.warehouse_id = w.id
            WHERE bw.branch_id = ? AND i.product_id = ? AND i.on_hand > i.reserved
            ORDER BY bw.is_primary DESC, w.id
        """, (branch_id, product["id"]))
        for warehouse in warehouses:
            count = min(remaining, warehouse["available"])
            allocations.setdefault(product["id"], []).append((warehouse, count))
            remaining -= count
            if not remaining:
                break
        if remaining:
            shortages.append({
                "sku": product["sku"], "requested": requested,
                "available": requested - remaining, "missing": remaining, "unit": product["unit"],
            })
    return allocations, shortages


def reserve_stock(db, order_id, lines, items, allocations):
    """Persist allocations already checked in the caller's write transaction."""
    reservations, reserved = {}, []
    for product, _ in lines:
        for warehouse, count in allocations[product["id"]]:
            warehouse_id = warehouse["id"]
            if warehouse_id not in reservations:
                reservations[warehouse_id] = db.execute("""
                    INSERT INTO inventory_reservations (order_id, warehouse_id) VALUES (?, ?)
                """, (order_id, warehouse_id)).lastrowid
            db.execute("INSERT INTO inventory_reservation_items VALUES (?, ?, ?)",
                       (reservations[warehouse_id], items[product["id"]], count))
            db.execute("""
                UPDATE inventory SET reserved = reserved + ?
                WHERE warehouse_id = ? AND product_id = ?
            """, (count, warehouse_id, product["id"]))
            reserved.append({
                "sku": product["sku"], "warehouse": warehouse["code"], "quantity": count,
                "unit": product["unit"], "available_after": warehouse["available"] - count,
            })
    return reserved


def target_warehouse(db, branch_id):
    return db.execute("""
        SELECT w.id, w.code FROM branch_warehouses AS bw JOIN warehouses AS w ON w.id = bw.warehouse_id
        WHERE bw.branch_id = ? ORDER BY bw.is_primary DESC, w.id LIMIT 1
    """, (branch_id,)).fetchone()


def open_shortages(db, order_number):
    order = find_order(db, order_number)
    if order is None or order["status"] != "awaiting_stock":
        return None, None, []
    rows = order_lines(db, order["id"])
    ids = {row["sku"]: row["id"] for row in rows}
    _, shortages = check_stock(db, order["branch_id"], [(row, row["quantity"]) for row in rows])
    open_items = {row[0] for row in db.execute("""
        SELECT product_id FROM demo_replenishments WHERE order_id = ? AND status <> 'received'
    """, (order["id"],))}
    shortages = [
        {**line, "product_id": ids[line["sku"]]} for line in shortages
        if ids[line["sku"]] not in open_items
    ]
    return order, target_warehouse(db, order["branch_id"]), shortages


def transfer_agent(db, event):
    number = event["order_number"]
    order, destination, shortages = open_shortages(db, number)
    if destination is None or not shortages:
        return Assessment("No uncovered transfer requirement.")
    eligible = {row["warehouse_id"] for row in source(db, "branch_warehouse_services") if row["can_transfer"]}
    if destination["id"] not in eligible:
        return Assessment(f"{destination['code']} has no transfer route in the warehouse mapping.")
    reorder_points = {(r["warehouse_id"], r["product_id"]): r["reorder_point"]
                      for r in source(db, "inventory")}
    fulfilling = {row[0] for row in db.execute(
        "SELECT warehouse_id FROM branch_warehouses WHERE branch_id = ?", (order["branch_id"],),
    )}
    drafts = []
    for item in shortages:
        candidates = db.execute("""
            SELECT w.id, w.code, i.on_hand - i.reserved AS available
            FROM inventory AS i JOIN warehouses AS w ON w.id = i.warehouse_id
            WHERE i.product_id = ? ORDER BY available DESC, w.id
        """, (item["product_id"],))
        for donor in candidates:
            buffer = reorder_points.get((donor["id"], item["product_id"]), 0)
            if donor["id"] in eligible - fulfilling and donor["available"] - buffer >= item["missing"]:
                payload = {
                    "order_number": number, "product_id": item["product_id"], "sku": item["sku"],
                    "quantity": item["missing"], "origin_id": donor["id"], "origin": donor["code"],
                    "destination_id": destination["id"], "destination": destination["code"],
                }
                drafts.append(Draft(
                    "start_transfer", f"{number}/{item['sku']}",
                    f"Transfer {item['missing']} {item['unit']} of {item['sku']} "
                    f"from {donor['code']} to {destination['code']}.", payload,
                ))
                break
    return Assessment(f"Found {len(drafts)} transfer option(s) for {number}.", tuple(drafts))


def start_transfer(db, approval, payload):
    candidates = transfer_agent(db, payload).drafts
    if not any(draft.payload == payload for draft in candidates):
        raise Blocked("The shortage, donor availability or transfer route changed.")
    order = find_order(db, payload["order_number"])
    db.execute("""
        UPDATE inventory SET on_hand = on_hand - ? WHERE warehouse_id = ? AND product_id = ?
    """, (payload["quantity"], payload["origin_id"], payload["product_id"]))
    db.execute("""
        INSERT INTO demo_replenishments
        VALUES (?, ?, ?, 'transfer', ?, ?, NULL, ?, 'in_transit')
    """, (approval["id"], order["id"], payload["product_id"], payload["destination_id"],
          payload["origin_id"], payload["quantity"]))
    save_outbound(db, approval["id"], "warehouse_demo", payload["origin"],
                  f"Transfer {payload['quantity']} {payload['sku']} to {payload['destination']}.")
    enqueue(db, f"receipt:{approval['id']}", "demo.receipt_reported", {"replenishment_id": approval["id"]})
    return {"transfer_status": "in_transit", "quantity": payload["quantity"]}


def receipt_agent(db, event):
    row = db.execute("""
        SELECT r.*, p.sku, w.code FROM demo_replenishments AS r
        JOIN products AS p ON p.id = r.product_id JOIN warehouses AS w ON w.id = r.destination_id
        WHERE r.id = ? AND r.status <> 'received'
    """, (event["replenishment_id"],)).fetchone()
    if row is None:
        return Assessment("No inbound quantity is awaiting posting.")
    return Assessment(f"Demo receiving reported {row['quantity']} {row['sku']} at {row['code']}.", (
        Draft("post_goods_receipt", str(row["id"]),
              f"Post receipt of {row['quantity']} {row['sku']} at {row['code']}.", dict(row)),
    ))


def reserve_waiting_orders(db, event_key):
    reserved = []
    for order in db.execute("SELECT * FROM sales_orders WHERE status = 'awaiting_stock' ORDER BY id").fetchall():
        rows = order_lines(db, order["id"])
        lines = [(row, row["quantity"]) for row in rows]
        allocations, shortages = check_stock(db, order["branch_id"], lines)
        if not shortages and lines:
            items = {row["id"]: row["item_id"] for row in rows}
            reserve_stock(db, order["id"], lines, items, allocations)
            db.execute("UPDATE sales_orders SET status = 'reserved' WHERE id = ?", (order["id"],))
            number = order["order_number"]
            enqueue(db, f"reserved:{order['id']}", "order.reserved", {"order_number": number})
            log(db, event_key, "Inventory", "automatic", f"Reserved stock for {number} after receipt.")
            reserved.append(number)
    return reserved


def post_goods_receipt(db, approval, payload):
    candidates = receipt_agent(db, {"replenishment_id": payload["id"]}).drafts
    if not candidates or candidates[0].payload != payload:
        raise Blocked("The inbound shipment already changed or was received.")
    db.execute("""
        INSERT INTO inventory (warehouse_id, product_id, on_hand, reserved) VALUES (?, ?, ?, 0)
        ON CONFLICT (warehouse_id, product_id) DO UPDATE SET on_hand = on_hand + excluded.on_hand
    """, (payload["destination_id"], payload["product_id"], payload["quantity"]))
    db.execute("UPDATE demo_replenishments SET status = 'received' WHERE id = ?", (payload["id"],))
    return {"receipt_status": "received", "quantity": payload["quantity"],
            "orders_reserved": reserve_waiting_orders(db, approval["event_key"])}


def stock_reconciliation_agent(db, event):
    inventory = source(db, "inventory")
    active = {r["reservation_id"] for r in source(db, "inventory_reservations") if r["status"] == "active"}
    reserved = defaultdict(int)
    for row in source(db, "inventory_reservation_items"):
        if row["reservation_id"] in active:
            reserved[row["warehouse_id"], row["product_id"]] += row["quantity"]
    last_movement = {}
    for row in sorted(source(db, "stock_movements"), key=lambda r: (r["movement_date"], r["movement_id"])):
        last_movement[row["warehouse_id"], row["product_id"]] = row["balance_after"]
    reservation_differences = balance_differences = 0
    for row in inventory:
        key = row["warehouse_id"], row["product_id"]
        reservation_differences += row["quantity_reserved"] != reserved[key]
        balance_differences += row["quantity_on_hand"] != last_movement.get(key)
    categories = {r["category_id"] for r in source(db, "product_categories") if r["is_active"]}
    uncategorized = sum(r["is_active"] and r["category_id"] not in categories for r in source(db, "products"))
    return Assessment(f"Source stock reconciliation: {balance_differences} movement balance differences, "
                      f"{reservation_differences} reservation differences and {uncategorized} missing categories.")

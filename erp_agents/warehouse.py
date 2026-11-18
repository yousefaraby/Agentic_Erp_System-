"""Warehouse agent: plan a route and dispatch reserved orders."""

from collections import defaultdict
from decimal import Decimal

from storage import Assessment, Blocked, Draft, enqueue, find_order, save_outbound, source


def shipping_plan(db, order):
    number = order["order_number"]
    client = next((row for row in source(db, "customers") if row["customer_id"] == order["customer_id"]), {})
    if not all(client.get(key) for key in ("address_line", "city", "region", "country")):
        return None
    destination = ", ".join(client[key] for key in ("address_line", "city", "region", "country"))
    weights = {r["product_id"]: Decimal(str(r["weight_kg"])) for r in source(db, "products")
               if r["weight_kg"] and r["weight_kg"] > 0}
    busy = {r["truck_id"] for r in source(db, "truck_trips")
            if r["trip_status"].casefold() == "in transit"}
    busy.update(row[0] for row in db.execute(
        "SELECT truck FROM demo_shipments WHERE status = 'in_transit' AND truck IS NOT NULL",
    ))
    trucks = source(db, "trucks")
    grouped = defaultdict(list)
    for row in db.execute("""
        SELECT r.warehouse_id, w.code, p.id AS product_id, p.sku, p.unit, ri.quantity
        FROM inventory_reservations AS r JOIN warehouses AS w ON w.id = r.warehouse_id
        JOIN inventory_reservation_items AS ri ON ri.reservation_id = r.id
        JOIN sales_order_items AS i ON i.id = ri.order_item_id
        JOIN products AS p ON p.id = i.product_id
        WHERE r.order_id = ? ORDER BY r.warehouse_id, p.sku
    """, (order["id"],)):
        if row["product_id"] not in weights:
            return None
        grouped[row["warehouse_id"], row["code"]].append(dict(row))
    shipments = []
    for (warehouse_id, code), items in grouped.items():
        weight = sum(weights[item["product_id"]] * item["quantity"] for item in items)
        truck = next((t["truck_id"] for t in trucks
                      if t["home_warehouse_code"] == code and t["service_status"].casefold() == "in service"
                      and t["truck_id"] not in busy and t["payload_capacity_kg"] >= weight), None)
        if truck:
            busy.add(truck)
        shipments.append({
            "warehouse_id": warehouse_id, "warehouse": code, "truck": truck,
            "carrier": "Cedarline fleet" if truck else "Demo parcel carrier",
            "weight_kg": float(weight), "items": items,
        })
    if not shipments:
        return None
    return {"order_number": number, "destination": destination, "shipments": shipments}


def warehouse_agent(db, event):
    number = event["order_number"]
    order = find_order(db, number)
    if order is None or order["status"] != "reserved":
        return Assessment(f"{number} has no reservation awaiting dispatch.")
    payload = shipping_plan(db, order)
    if payload is None:
        return Assessment(f"{number} has incomplete routing, weight or reservation data.", (
            Draft("review_intake_exception", number, f"Review routing for {number}.", {
                "reason": "A complete destination, item weight and active reservation are required.",
            }),
        ))
    warehouses = ", ".join(shipment["warehouse"] for shipment in payload["shipments"])
    return Assessment(f"Picked a route for {number} from {warehouses}.", (
        Draft("dispatch_order", number, f"Send pick instructions and dispatch {number} from {warehouses}.", payload),
    ))


def dispatch_order(db, approval, payload):
    order = find_order(db, payload["order_number"])
    if order is None or order["status"] != "reserved" or shipping_plan(db, order) != payload:
        raise Blocked("The reservation, destination or fleet availability changed.")
    references = []
    for shipment in payload["shipments"]:
        for item in shipment["items"]:
            count = item["quantity"]
            change = db.execute("""
                UPDATE inventory SET on_hand = on_hand - ?, reserved = reserved - ?
                WHERE warehouse_id = ? AND product_id = ? AND reserved >= ?
            """, (count, count, shipment["warehouse_id"], item["product_id"], count))
            if change.rowcount != 1:
                raise Blocked("The reserved stock is no longer available.")
        references.append(db.execute("""
            INSERT INTO demo_shipments
                (order_id, warehouse_id, truck, destination, weight_kg, status, approval_id)
            VALUES (?, ?, ?, ?, ?, 'in_transit', ?)
        """, (order["id"], shipment["warehouse_id"], shipment["truck"], payload["destination"],
              shipment["weight_kg"], approval["id"])).lastrowid)
    db.execute("UPDATE sales_orders SET status = 'shipped' WHERE id = ?", (order["id"],))
    save_outbound(db, approval["id"], "warehouse_demo",
                  ", ".join(row["warehouse"] for row in payload["shipments"]),
                  f"Pick and dispatch {order['order_number']}.")
    enqueue(db, f"delivery:{order['id']}", "demo.delivery_reported", {"order_number": order["order_number"]})
    return {"shipments": references, "order_status": "shipped", "warehouse_instruction": "simulated"}

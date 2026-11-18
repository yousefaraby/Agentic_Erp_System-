"""Logistics agent: review shipments and confirm delivery."""

from storage import Assessment, Blocked, Draft, enqueue, find_order, source


def delivery_agent(db, event):
    number = event["order_number"]
    order = find_order(db, number)
    if order is None or order["status"] != "shipped":
        return Assessment(f"{number} has no delivery to confirm.")
    return Assessment(f"Demo carrier reported delivery for {number}.", (
        Draft("confirm_delivery", number, f"Record delivery of {number}.", {"order_number": number}),
    ))


def confirm_delivery(db, approval, payload):
    order = find_order(db, payload["order_number"])
    if order is None or order["status"] != "shipped":
        raise Blocked("The order is no longer in transit.")
    change = db.execute("UPDATE demo_shipments SET status = 'delivered' WHERE order_id = ? AND status = 'in_transit'",
                        (order["id"],))
    if change.rowcount < 1:
        raise Blocked("There are no in-transit shipments for this order.")
    db.execute("UPDATE sales_orders SET status = 'delivered' WHERE id = ?", (order["id"],))
    enqueue(db, f"delivered:{order['id']}", "order.delivered", payload)
    return {"order_status": "delivered", "shipment_count": change.rowcount}


def logistics_monitor(db, event):
    rows = db.execute("""
        SELECT table_name, count(*) FROM source_data
        WHERE table_name IN ('trucks', 'truck_trips', 'truck_loads', 'shipments', 'stock_transfers')
        GROUP BY table_name
    """).fetchall()
    counts = dict(rows)
    late = db.execute("""
        SELECT count(*) FROM source_data
        WHERE table_name = 'shipments' AND json_extract(payload, '$.status') = 'in_transit'
        AND substr(json_extract(payload, '$.promised_date'), 1, 10) < ?
    """, (event["as_of"],)).fetchone()[0]
    transfer_headers = {r["transfer_id"] for r in source(db, "stock_transfers")}
    unlinked = sum(r["transfer_id"] not in transfer_headers for r in source(db, "stock_transfer_items"))
    return Assessment(f"Scanned {counts.get('trucks', 0)} trucks, {counts.get('truck_trips', 0)} trips, "
                      f"{counts.get('truck_loads', 0)} loads and {counts.get('stock_transfers', 0)} transfers. "
                      f"{late} source shipments past the promised date and {unlinked} unlinked transfer items.")

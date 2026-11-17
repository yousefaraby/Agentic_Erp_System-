"""Payment reconciliation and approval-gated demo refunds."""

from collections import defaultdict

from .inventory import reserve_waiting_orders
from storage import Assessment, Blocked, Draft, minor_units, source


def reconciliation_agent(db, event):
    payments = source(db, "payments")
    source_refunds = source(db, "payment_refunds")
    pending = sum(row["status"] == "pending" for row in payments)
    completed_refunds = sum(row["status"] == "completed" for row in source_refunds)
    refunded = defaultdict(int)
    for row in source_refunds:
        if row["status"] == "completed":
            refunded[row["return_id"]] += minor_units(row["amount"])
    discrepancies = sum(row["status"] == "refunded" and minor_units(row["refund_amount"]) != refunded[row["return_id"]]
                        for row in source(db, "returns"))
    return Assessment(f"{pending} source payments pending settlement, {completed_refunds} completed historical refunds "
                      f"and {discrepancies} return-to-refund balance differences.")


def refund_agent(db, event):
    reference = event["return_reference"]
    if db.execute("SELECT 1 FROM demo_refunds WHERE return_reference = ?", (reference,)).fetchone():
        return Assessment(f"Refund {reference} is already recorded.")
    item = next((r for r in source(db, "sales_order_items")
                 if r["sales_order_item_id"] == event["order_item_id"]), None)
    if not item:
        return Assessment(f"{reference} has no matching sales item.")
    order = db.execute("SELECT * FROM sales_orders WHERE id = ?", (item["sales_order_id"],)).fetchone()
    if not order or order["status"] != "delivered":
        return Assessment(f"{reference} requires a delivered order.")
    payment = next((r for r in source(db, "payments") if r["payment_id"] == event["payment_id"]
                    and r["sales_order_id"] == order["id"] and r["status"] == "completed"), None)
    if not payment:
        return Assessment(f"{reference} has no settled payment tied to this order.")
    prior_units = sum(row["quantity"] for row in source(db, "return_items")
                      if row["sales_order_item_id"] == item["sales_order_item_id"])
    prior_units += db.execute("""
        SELECT coalesce(sum(quantity), 0) FROM demo_refunds WHERE order_item_id = ?
    """, (item["sales_order_item_id"],)).fetchone()[0]
    quantity = event["quantity"]
    if not 0 < quantity <= item["quantity_fulfilled"] - prior_units:
        return Assessment(f"{reference} exceeds the fulfilled quantity remaining for return.")
    prior_paid = sum(minor_units(row["amount"]) for row in source(db, "payment_refunds")
                     if row["payment_id"] == payment["payment_id"] and row["status"] == "completed")
    prior_paid += db.execute("""
        SELECT coalesce(sum(amount_minor), 0) FROM demo_refunds WHERE payment_id = ?
    """, (payment["payment_id"],)).fetchone()[0]
    # Compute the incremental portion of the original tax-inclusive line amount in cents.
    line_total = minor_units(item["line_total"])
    ordered = item["quantity"]
    amount = (line_total * (prior_units + quantity) // ordered) - (line_total * prior_units // ordered)
    if not 0 < amount <= minor_units(payment["amount"]) - prior_paid:
        return Assessment(f"{reference} exceeds the refundable balance of the original payment.")
    location = db.execute("SELECT code FROM warehouses WHERE id = ?", (item["warehouse_id"],)).fetchone()
    warehouse_id = item["warehouse_id"] if event["restock"] and location else None
    payload = {
        "return_reference": reference, "order_number": order["order_number"],
        "order_item_id": item["sales_order_item_id"], "payment_id": payment["payment_id"],
        "quantity": quantity, "product_id": item["product_id"], "warehouse_id": warehouse_id,
        "restock": bool(event["restock"]), "warehouse": location["code"] if warehouse_id else None,
        "amount_minor": amount, "currency": order["currency"],
    }
    return Assessment(f"Matched {reference} to a delivered line and a settled payment.", (
        Draft("record_refund", reference,
              f"Record a simulated {order['currency']} {amount / 100:,.2f} refund for {order['order_number']}.",
              payload),
    ))


def record_refund(db, approval, payload):
    drafts = refund_agent(db, payload).drafts
    if not drafts or drafts[0].payload != payload:
        raise Blocked("The refundable amount, inspection or linked payment changed.")
    db.execute("""
        INSERT INTO demo_refunds VALUES (?, ?, ?, ?, ?, ?)
    """, (approval["id"], payload["return_reference"], payload["payment_id"], payload["order_item_id"],
          payload["quantity"], payload["amount_minor"]))
    reserved = []
    if payload["warehouse_id"]:
        db.execute("""
            INSERT INTO inventory (warehouse_id, product_id, on_hand, reserved) VALUES (?, ?, ?, 0)
            ON CONFLICT (warehouse_id, product_id) DO UPDATE SET on_hand = on_hand + excluded.on_hand
        """, (payload["warehouse_id"], payload["product_id"], payload["quantity"]))
        reserved = reserve_waiting_orders(db, approval["event_key"])
    return {"refund": f"DEMO-RF-{approval['id']:05d}", "amount_minor": payload["amount_minor"],
            "payment": "simulated", "restocked": bool(payload["warehouse_id"]), "orders_reserved": reserved}

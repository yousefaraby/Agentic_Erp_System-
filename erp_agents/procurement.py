"""Supplier selection, purchase proposals and overdue supplier follow-up."""

from collections import defaultdict

from .inventory import open_shortages
from storage import Assessment, Blocked, Draft, enqueue, find_order, minor_units, save_outbound, source


def active_suppliers(db):
    return {r["supplier_id"]: r for r in source(db, "suppliers") if r["is_active"] and r["email"]}


def supplier_quotes(db, day, product_id, needed):
    suppliers = active_suppliers(db)
    quotes = []
    for offer in source(db, "supplier_products"):
        if (offer["product_id"] != product_id or offer["supplier_id"] not in suppliers
                or offer["valid_from"][:10] > day
                or (offer["valid_to"] and offer["valid_to"][:10] < day)):
            continue
        supplier = suppliers[offer["supplier_id"]]
        quantity = max(needed, offer["min_order_qty"])
        cost = minor_units(offer["unit_cost"])
        rank = (cost * quantity, offer["lead_time_days"], not offer["is_preferred"], offer["supplier_product_id"])
        quotes.append((rank, {
            "supplier_id": supplier["supplier_id"], "supplier": supplier["name"],
            "supplier_sku": offer["supplier_sku"], "recipient": supplier["email"],
            "quantity": quantity, "unit_cost_minor": cost, "total_minor": cost * quantity,
            "currency": "USD", "lead_days": offer["lead_time_days"],
        }))
    return min(quotes, key=lambda quote: quote[0])[1] if quotes else None


def purchase_agent(db, event):
    number, day = event["order_number"], event["as_of"]
    order, destination, shortages = open_shortages(db, number)
    if destination is None or not shortages:
        return Assessment("No uncovered purchasing requirement.")
    drafts = []
    for item in shortages:
        quote = supplier_quotes(db, day, item["product_id"], item["missing"])
        if quote is None:
            continue
        quantity, total = quote["quantity"], quote["total_minor"]
        payload = {
            **quote,
            "order_number": number, "as_of": day, "product_id": item["product_id"], "sku": item["sku"],
            "destination_id": destination["id"], "destination": destination["code"],
            "unit": item["unit"],
        }
        drafts.append(Draft(
            "place_purchase_order", f"{number}/{item['sku']}",
            f"Order {quantity} {item['unit']} of {item['sku']} from {quote['supplier']} "
            f"for ${total / 100:,.2f} before tax and freight.", payload,
        ))
    return Assessment(f"Found {len(drafts)} supplier option(s) for {number}.", tuple(drafts))


def place_purchase_order(db, approval, payload):
    if not any(draft.payload == payload for draft in purchase_agent(db, payload).drafts):
        raise Blocked("The purchase no longer matches the shortage or supplier quote.")
    order = find_order(db, payload["order_number"])
    po_number = f"DEMO-PO-{approval['id']:05d}"
    db.execute("""
        INSERT INTO demo_replenishments
        VALUES (?, ?, ?, 'purchase', ?, NULL, ?, ?, 'ordered')
    """, (approval["id"], order["id"], payload["product_id"], payload["destination_id"],
          payload["supplier_id"], payload["quantity"]))
    save_outbound(db, approval["id"], "email_demo", payload["recipient"],
                  f"{po_number}: {payload['quantity']} {payload['unit']} of {payload['supplier_sku']} "
                  f"for {payload['destination']}, unit cost USD {payload['unit_cost_minor'] / 100:.2f}.")
    enqueue(db, f"receipt:{approval['id']}", "demo.receipt_reported", {"replenishment_id": approval["id"]})
    return {"purchase_order": po_number, "supplier_message": "simulated"}


def overdue_supplier_agent(db, event):
    suppliers = active_suppliers(db)
    pending = {
        r["purchase_order_id"]: r for r in source(db, "purchase_orders")
        if r["status"] in {"ordered", "partially_received"} and r["expected_date"]
        and r["expected_date"][:10] < event["as_of"] and r["supplier_id"] in suppliers
    }
    quantities = defaultdict(list)
    order_items = source(db, "purchase_order_items")
    for item in order_items:
        balance = item["quantity_ordered"] - item["quantity_received"]
        if item["purchase_order_id"] in pending and balance > 0:
            quantities[item["purchase_order_id"]].append((item["product_id"], balance))
    catalog = {r["product_id"]: r for r in source(db, "products")}
    drafts = []
    for po_id, purchase in pending.items():
        lines = quantities[po_id]
        if not lines:
            continue
        supplier = suppliers[purchase["supplier_id"]]
        number = purchase["po_number"]
        open_items = ", ".join(f"{balance} {catalog[product_id]['unit_of_measure']} "
                               f"{catalog[product_id]['sku']}" for product_id, balance in lines)
        drafts.append(Draft("follow_up_supplier", number, f"Ask {supplier['name']} for an ETA on {number}.", {
            "po_number": number, "channel": "email_demo", "recipient": supplier["email"],
            "expected_date": purchase["expected_date"][:10],
            "message": f"Please confirm the delivery date for the outstanding items on {number}: {open_items}.",
        }))
    receipt_ids = {r["receipt_id"] for r in source(db, "goods_receipts")}
    received = defaultdict(int)
    for item in source(db, "goods_receipt_items"):
        if item["receipt_id"] in receipt_ids:
            received[item["po_item_id"]] += item["quantity_received"]
    discrepancies = sum(r["quantity_received"] != received[r["po_item_id"]] for r in order_items)
    return Assessment(f"{len(drafts)} overdue purchase orders with open items; "
                      f"{discrepancies} source receipt quantity differences.", tuple(drafts))

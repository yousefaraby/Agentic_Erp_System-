"""Communications agent: prepare and export customer emails."""

from email.message import EmailMessage
from email.policy import SMTP
import json

from storage import Assessment, Blocked, Draft, find_order, save_outbound


def money(minor, currency):
    whole, cents = divmod(minor, 100)
    return f"{currency} {whole:,}.{cents:02d}"


def plan_confirmation(db, event):
    intake = event["intake"]
    number = intake["order_number"]
    order = find_order(db, number)
    if not order or order["status"] == "cancelled":
        return Assessment(f"{number} is unavailable for confirmation.")
    customer = db.execute(
        "SELECT name, email FROM customers WHERE id = ? AND active = 1", (order["customer_id"],),
    ).fetchone()
    if not customer or not customer["email"] or customer["email"] != event["sender"]:
        return Assessment(f"The registered address for {number} no longer matches the order email.")
    branch = db.execute("SELECT code FROM branches WHERE id = ?", (order["branch_id"],)).fetchone()
    lines = db.execute("""
        SELECT p.sku, p.unit, i.quantity, i.unit_price_minor
        FROM sales_order_items AS i JOIN products AS p ON p.id = i.product_id
        WHERE i.order_id = ? ORDER BY i.id
    """, (order["id"],)).fetchall()
    total = sum(row["quantity"] * row["unit_price_minor"] for row in lines)
    if not lines or total != order["total_minor"]:
        return Assessment(f"{number} has item totals that require reconciliation.")

    currency = order["currency"]
    items = [
        f"- {row['sku']}: {row['quantity']} {row['unit']} × {money(row['unit_price_minor'], currency)} "
        f"= {money(row['quantity'] * row['unit_price_minor'], currency)}"
        for row in lines
    ]
    if intake["status"] == "reserved":
        stock = ["Stock at booking: all requested quantities were reserved."]
    else:
        stock = ["Stock at booking: the order was placed on hold pending stock."]
        stock.extend(
            f"- {row['sku']}: {row['requested']} {row['unit']} ordered, "
            f"{row['available']} available, {row['missing']} short."
            for row in intake["shortages"]
        )
    subject = f"Cedarline order confirmation {number}"
    message = "\n".join([
        f"Hello {customer['name']},", "", "We received your Cedarline order.", "",
        f"Order: {number}", f"Branch: {branch['code']}", "", "Items and prices", *items, "",
        f"Order total at catalog prices: {money(total, currency)}", "",
        *stock, "", "Thank you,", "Cedarline Orders",
    ])
    draft = Draft("send_order_confirmation", number, f"Email the itemized confirmation for {number}.", {
        "order_number": number, "channel": "email_demo", "recipient": customer["email"],
        "subject": subject, "message": message, "currency": currency, "total_minor": total,
    })
    return Assessment(f"Prepared the order confirmation for {number}.", (draft,))


def send_confirmation(db, approval, payload):
    event = db.execute("SELECT payload FROM company_events WHERE event_key = ?", (approval["event_key"],)).fetchone()
    drafts = plan_confirmation(db, json.loads(event["payload"])).drafts
    if not drafts or drafts[0].payload != payload:
        raise Blocked("The order confirmation no longer matches the order or customer.")
    email_text = f"Subject: {payload['subject']}\n\n{payload['message']}"
    receipt = save_outbound(db, approval["id"], payload["channel"], payload["recipient"], email_text)
    return {**receipt, "subject": payload["subject"], "order_number": payload["order_number"]}


def outbound_email(db, approval_id):
    row = db.execute("""
        SELECT o.recipient, o.message, a.summary, a.payload
        FROM outbound_messages AS o JOIN approvals AS a ON a.id = o.approval_id
        WHERE o.approval_id = ? AND o.channel = 'email_demo' AND o.status = 'simulated'
        AND a.status = 'executed'
    """, (approval_id,)).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    email = EmailMessage(policy=SMTP)
    email["From"] = "Cedarline Demo <orders@cedarline.example>"
    email["To"] = row["recipient"]
    email["Subject"] = " ".join((payload.get("subject") or row["summary"]).splitlines())
    email["X-Cedarline-Mode"] = "simulated"
    email.set_content(payload.get("message") or row["message"])
    return email.as_bytes()


def customer_agent(db, event):
    number = event["order_number"]
    order = find_order(db, number)
    if order is None or order["status"] != "delivered":
        return Assessment(f"{number} has no confirmed delivery to announce.")
    customer = db.execute("SELECT email FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    if not customer or not customer["email"]:
        return Assessment(f"No registered contact is available for {number}.")
    payload = {"order_number": number, "channel": "email_demo", "recipient": customer["email"],
               "message": f"Cedarline order {number} has been delivered."}
    draft = Draft("notify_customer", number, f"Send a demo delivery notice for {number}.", payload)
    return Assessment(f"Prepared a delivery notice for {number}.", (draft,))


def send_demo_message(db, approval, payload):
    if approval["kind"] == "notify_customer":
        candidates = customer_agent(db, payload).drafts
        if not candidates or candidates[0].payload != payload:
            raise Blocked("The customer's delivery status or address changed.")
    return save_outbound(db, approval["id"], payload["channel"], payload["recipient"], payload["message"])

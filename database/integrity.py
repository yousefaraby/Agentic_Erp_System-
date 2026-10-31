"""Read-only reconciliation against imported rows and recorded source counts."""
from sqlalchemy import text
CHECKS = {
 "branch_staff": "SELECT count(*) FROM erp.sales_orders o JOIN erp.employees e USING(employee_id) WHERE o.branch_id<>e.branch_id",
 "fulfilment_scope": "SELECT count(*) FROM erp.sales_order_items i JOIN erp.sales_orders o USING(sales_order_id) WHERE NOT EXISTS(SELECT 1 FROM erp.branch_warehouse_services s WHERE s.branch_id=o.branch_id AND s.warehouse_id=i.warehouse_id AND s.can_fulfill AND o.order_date::date>=s.valid_from AND (s.valid_to IS NULL OR o.order_date::date<=s.valid_to))",
 "stock_ledger": "SELECT count(*) FROM erp.inventory i FULL JOIN (SELECT warehouse_id,product_id,sum(quantity_change) qty FROM erp.stock_movements GROUP BY 1,2) m USING(warehouse_id,product_id) WHERE i.inventory_id IS NULL OR i.quantity_on_hand<>coalesce(m.qty,0)",
 "ledger_sequence": "SELECT count(*) FROM (SELECT balance_after,sum(quantity_change) OVER(PARTITION BY warehouse_id,product_id ORDER BY movement_date,movement_id ROWS UNBOUNDED PRECEDING) expected FROM erp.stock_movements) x WHERE balance_after<>expected",
 "reservations": "SELECT count(*) FROM erp.inventory i WHERE i.quantity_reserved<>coalesce((SELECT sum(l.quantity) FROM erp.inventory_reservation_items l JOIN erp.inventory_reservations r USING(reservation_id) WHERE r.status='active' AND l.warehouse_id=i.warehouse_id AND l.product_id=i.product_id),0)",
 "receipt_quantity": "SELECT count(*) FROM erp.purchase_order_items i WHERE i.quantity_received<>coalesce((SELECT sum(g.quantity_received) FROM erp.goods_receipt_items g WHERE g.po_item_id=i.po_item_id),0)",
 "receipt_identity": "SELECT count(*) FROM erp.goods_receipt_items i JOIN erp.goods_receipts r USING(receipt_id) JOIN erp.purchase_order_items l USING(po_item_id) JOIN erp.purchase_orders p ON p.purchase_order_id=l.purchase_order_id WHERE i.product_id<>l.product_id OR r.purchase_order_id<>p.purchase_order_id OR r.warehouse_id<>p.warehouse_id",
 "sales_totals": "SELECT count(*) FROM erp.sales_orders o WHERE o.subtotal<>coalesce((SELECT sum(i.line_subtotal) FROM erp.sales_order_items i WHERE i.sales_order_id=o.sales_order_id),0) OR o.total_amount<>coalesce((SELECT sum(i.line_total) FROM erp.sales_order_items i WHERE i.sales_order_id=o.sales_order_id),0)+o.shipping_amount",
 "po_totals": "SELECT count(*) FROM erp.purchase_orders p WHERE p.subtotal<>coalesce((SELECT sum(i.line_total) FROM erp.purchase_order_items i WHERE i.purchase_order_id=p.purchase_order_id),0)",
 "refund_total": "SELECT count(*) FROM erp.returns r WHERE r.refund_amount<>coalesce((SELECT sum(i.line_refund) FROM erp.return_items i WHERE i.return_id=r.return_id),0) OR r.refund_amount<>coalesce((SELECT sum(f.amount) FROM erp.payment_refunds f WHERE f.return_id=r.return_id AND f.status='completed'),0)",
 "refund_limit": "SELECT count(*) FROM erp.payments p WHERE p.amount<coalesce((SELECT sum(f.amount) FROM erp.payment_refunds f WHERE f.payment_id=p.payment_id AND f.status='completed'),0)",
 "return_identity": "SELECT count(*) FROM erp.return_items i JOIN erp.returns r USING(return_id) JOIN erp.sales_order_items l USING(sales_order_item_id) JOIN erp.sales_orders o ON o.sales_order_id=l.sales_order_id WHERE i.product_id<>l.product_id OR r.sales_order_id<>o.sales_order_id OR r.customer_id<>o.customer_id",
 "refund_identity": "SELECT count(*) FROM erp.payment_refunds f JOIN erp.payments p USING(payment_id) JOIN erp.returns r USING(return_id) WHERE p.sales_order_id<>r.sales_order_id OR f.refund_date<r.return_date",
 "reservation_identity": "SELECT count(*) FROM erp.inventory_reservation_items i JOIN erp.inventory_reservations r USING(reservation_id) JOIN erp.sales_order_items l USING(sales_order_item_id) WHERE r.sales_order_id<>l.sales_order_id OR r.warehouse_id<>i.warehouse_id OR i.warehouse_id<>l.warehouse_id OR i.product_id<>l.product_id OR i.quantity>l.quantity-l.quantity_fulfilled",
}
def validate(conn):
    failures = {}
    for name, sql in CHECKS.items():
        n = conn.execute(text(sql)).scalar_one()
        if n: failures[name] = n
    files = conn.execute(text("SELECT table_name,row_count FROM provenance.import_files")).all()
    for name, expected in files:
        if not name.replace("_", "").isalnum(): raise ValueError("Invalid provenance table name")
        if name == "inventory":
            expected += conn.execute(text("SELECT count(*) FROM provenance.derived_inventory_rows")).scalar_one()
        actual = conn.execute(text(f'SELECT count(*) FROM erp."{name}"')).scalar_one()
        if actual != expected: failures["count:"+name] = (expected,actual)
    if failures: raise ValueError(f"Database integrity failures: {failures}")
    return {"checks":len(CHECKS)+len(files), "failures":0}

if __name__ == "__main__":
    import os
    from sqlalchemy import create_engine
    with create_engine(os.environ["ERP_DATABASE_URL"]).connect() as conn:
        if not conn.execute(text("SELECT count(*) FROM provenance.import_batches")).scalar_one():
            raise SystemExit("No dataset has been imported")
        print(validate(conn))

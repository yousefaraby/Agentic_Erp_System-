"""Automated validation of the generated dataset.

Runs a battery of SQL checks against the loaded database and prints a report of
passed and failed checks with explanations. Essential checks (foreign keys,
uniqueness, financial totals, inventory and transfer reconciliation, chronology)
must all pass for the dataset to be reported as valid.

Usage:
    python validate_data.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import CONFIG
from db import get_connection
from load_data import TABLE_ORDER

RESULTS: list["Check"] = []


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    essential: bool = True


def _scalar(cur, sql: str, params: tuple = ()) -> object:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _zero_ok(cur, name: str, sql: str, params: tuple = (), essential: bool = True,
             note: str = "") -> None:
    n = int(_scalar(cur, sql, params))
    RESULTS.append(Check(name, n == 0,
                         f"{n} offending row(s). {note}".strip(), essential))


def _count_ok(cur, name: str, sql: str, minimum: int = 0, maximum: int | None = None,
              essential: bool = True, note: str = "") -> None:
    n = int(_scalar(cur, sql))
    ok = n >= minimum and (maximum is None or n <= maximum)
    RESULTS.append(Check(name, ok,
                         f"count={n} (expected >= {minimum}"
                         + (f", <= {maximum}" if maximum is not None else "") + f"). {note}".strip(),
                         essential))


def run_validation() -> bool:
    RESULTS.clear()
    with get_connection() as conn, conn.cursor() as cur:
        # ---------------------------------------------------------------
        # 1. Row counts
        # ---------------------------------------------------------------
        for table in TABLE_ORDER:
            n = int(_scalar(cur, f"SELECT count(*) FROM erp.{table}"))
            RESULTS.append(Check(f"rowcount:{table}", n > 0, f"{n} rows", essential=False))

        _count_ok(cur, "rowcount:branches>=6",
                  "SELECT count(*) FROM erp.branches", minimum=6, note="1 HQ + 5 branches")
        _count_ok(cur, "rowcount:warehouses>=3",
                  "SELECT count(*) FROM erp.warehouses", minimum=3)
        _count_ok(cur, "rowcount:sales_orders>=1000",
                  "SELECT count(*) FROM erp.sales_orders", minimum=1000)

        # ---------------------------------------------------------------
        # 2. Foreign key integrity (explicit orphan checks)
        # ---------------------------------------------------------------
        _zero_ok(cur, "fk:sales_orders.branch",
                 "SELECT count(*) FROM erp.sales_orders o "
                 "LEFT JOIN erp.branches b ON b.branch_id=o.branch_id WHERE b.branch_id IS NULL")
        _zero_ok(cur, "fk:sales_orders.customer",
                 "SELECT count(*) FROM erp.sales_orders o "
                 "LEFT JOIN erp.customers c ON c.customer_id=o.customer_id "
                 "WHERE c.customer_id IS NULL")
        _zero_ok(cur, "fk:sales_order_items.product",
                 "SELECT count(*) FROM erp.sales_order_items i "
                 "LEFT JOIN erp.products p ON p.product_id=i.product_id WHERE p.product_id IS NULL")
        _zero_ok(cur, "fk:inventory.warehouse_product",
                 "SELECT count(*) FROM erp.inventory i "
                 "LEFT JOIN erp.warehouses w ON w.warehouse_id=i.warehouse_id "
                 "LEFT JOIN erp.products p ON p.product_id=i.product_id "
                 "WHERE w.warehouse_id IS NULL OR p.product_id IS NULL")
        _zero_ok(cur, "fk:stock_movements.warehouse_product",
                 "SELECT count(*) FROM erp.stock_movements m "
                 "LEFT JOIN erp.warehouses w ON w.warehouse_id=m.warehouse_id "
                 "LEFT JOIN erp.products p ON p.product_id=m.product_id "
                 "WHERE w.warehouse_id IS NULL OR p.product_id IS NULL")
        _zero_ok(cur, "fk:purchase_order_items.po",
                 "SELECT count(*) FROM erp.purchase_order_items i "
                 "LEFT JOIN erp.purchase_orders o ON o.purchase_order_id=i.purchase_order_id "
                 "WHERE o.purchase_order_id IS NULL")
        _zero_ok(cur, "fk:goods_receipt_items.po_item",
                 "SELECT count(*) FROM erp.goods_receipt_items i "
                 "LEFT JOIN erp.purchase_order_items p ON p.po_item_id=i.po_item_id "
                 "WHERE p.po_item_id IS NULL")

        # ---------------------------------------------------------------
        # 3. Duplicate identifiers
        # ---------------------------------------------------------------
        for col, table in [("order_number", "sales_orders"), ("po_number", "purchase_orders"),
                           ("transfer_number", "stock_transfers"), ("return_number", "returns"),
                           ("payment_number", "payments"), ("shipment_number", "shipments"),
                           ("sku", "products"), ("supplier_code", "suppliers"),
                           ("customer_code", "customers")]:
            _zero_ok(cur, f"unique:{table}.{col}",
                     f"SELECT count(*) FROM (SELECT {col} FROM erp.{table} "
                     f"GROUP BY {col} HAVING count(*) > 1) d")

        # ---------------------------------------------------------------
        # 4. Sales order total calculations
        # ---------------------------------------------------------------
        _zero_ok(cur, "sales:subtotal_matches_items",
                 """
                 SELECT count(*) FROM erp.sales_orders o
                 WHERE o.subtotal <> COALESCE(
                     (SELECT sum(i.line_subtotal) FROM erp.sales_order_items i
                      WHERE i.sales_order_id = o.sales_order_id), 0)
                 """)
        _zero_ok(cur, "sales:discount_matches_items",
                 """
                 SELECT count(*) FROM erp.sales_orders o
                 WHERE o.discount_amount <> COALESCE(
                     (SELECT sum(round(i.line_subtotal * i.discount_rate, 2))
                      FROM erp.sales_order_items i
                      WHERE i.sales_order_id = o.sales_order_id), 0)
                 """)
        _zero_ok(cur, "sales:tax_matches_items",
                 """
                 SELECT count(*) FROM erp.sales_orders o
                 WHERE o.tax_amount <> COALESCE(
                     (SELECT sum(round((i.line_subtotal
                         - round(i.line_subtotal * i.discount_rate, 2)) * i.tax_rate, 2))
                      FROM erp.sales_order_items i
                      WHERE i.sales_order_id = o.sales_order_id), 0)
                 """)
        _zero_ok(cur, "sales:total_equals_components",
                 """
                 SELECT count(*) FROM erp.sales_orders
                 WHERE total_amount <> subtotal - discount_amount + tax_amount + shipping_amount
                 """)
        _zero_ok(cur, "sales:fulfilled_leq_ordered",
                 "SELECT count(*) FROM erp.sales_order_items WHERE quantity_fulfilled > quantity")

        # ---------------------------------------------------------------
        # 5. Purchase order total calculations
        # ---------------------------------------------------------------
        _zero_ok(cur, "po:subtotal_matches_items",
                 """
                 SELECT count(*) FROM erp.purchase_orders o
                 WHERE o.subtotal <> COALESCE(
                     (SELECT sum(i.line_total) FROM erp.purchase_order_items i
                      WHERE i.purchase_order_id = o.purchase_order_id), 0)
                 """)
        _zero_ok(cur, "po:total_equals_components",
                 "SELECT count(*) FROM erp.purchase_orders "
                 "WHERE total_amount <> subtotal + tax_amount + shipping_amount")
        _zero_ok(cur, "po:received_leq_ordered",
                 "SELECT count(*) FROM erp.purchase_order_items WHERE quantity_received > quantity_ordered")
        _zero_ok(cur, "po:received_matches_receipts",
                 """
                 SELECT count(*) FROM erp.purchase_order_items i
                 WHERE i.quantity_received <> COALESCE(
                     (SELECT sum(g.quantity_received) FROM erp.goods_receipt_items g
                      WHERE g.po_item_id = i.po_item_id), 0)
                 """)

        # ---------------------------------------------------------------
        # 6. Inventory reconciliation (balance == sum of movements)
        # ---------------------------------------------------------------
        _zero_ok(cur, "inventory:balance_reconciles_movements",
                 """
                 SELECT count(*) FROM (
                     SELECT i.warehouse_id, i.product_id, i.quantity_on_hand,
                            COALESCE((SELECT sum(m.quantity_change)
                                      FROM erp.stock_movements m
                                      WHERE m.warehouse_id = i.warehouse_id
                                        AND m.product_id = i.product_id), 0) AS ledger
                     FROM erp.inventory i
                 ) t WHERE t.quantity_on_hand <> t.ledger
                 """)
        _zero_ok(cur, "inventory:nonnegative",
                 "SELECT count(*) FROM erp.inventory WHERE quantity_on_hand < 0")
        _zero_ok(cur, "movements:balance_after_nonnegative",
                 "SELECT count(*) FROM erp.stock_movements WHERE balance_after < 0")
        _zero_ok(cur, "movements:traceable_reference",
                 """
                 SELECT count(*) FROM erp.stock_movements
                 WHERE movement_type <> 'initial' AND reference_id IS NULL
                 """, note="every non-opening movement must reference its transaction")

        # ---------------------------------------------------------------
        # 7. Completed transfer reconciliation
        # ---------------------------------------------------------------
        _zero_ok(cur, "transfers:completed_have_received_date",
                 "SELECT count(*) FROM erp.stock_transfers "
                 "WHERE status = 'completed' AND received_date IS NULL")
        _zero_ok(cur, "transfers:received_leq_shipped",
                 "SELECT count(*) FROM erp.stock_transfer_items WHERE quantity_received > quantity_shipped")
        _zero_ok(cur, "transfers:shipped_leq_requested",
                 "SELECT count(*) FROM erp.stock_transfer_items WHERE quantity_shipped > quantity_requested")
        _zero_ok(cur, "transfers:pending_moved_no_stock",
                 """
                 SELECT count(*) FROM erp.stock_transfers t
                 WHERE t.status IN ('pending','rejected','cancelled','approved')
                   AND EXISTS (SELECT 1 FROM erp.stock_movements m
                               WHERE m.reference_type = 'stock_transfer'
                                 AND m.reference_id = t.transfer_id)
                 """, note="only completed dispatches may move stock")
        _zero_ok(cur, "transfers:source_differs_destination",
                 "SELECT count(*) FROM erp.stock_transfers "
                 "WHERE source_warehouse_id = destination_warehouse_id")
        _zero_ok(cur, "transfers:completed_have_movements",
                 """
                 SELECT count(*) FROM erp.stock_transfers t
                 WHERE t.status = 'completed'
                   AND NOT EXISTS (SELECT 1 FROM erp.stock_movements m
                                   WHERE m.reference_type='stock_transfer'
                                     AND m.reference_id=t.transfer_id
                                     AND m.movement_type='transfer_in')
                 """)

        # ---------------------------------------------------------------
        # 8. Status / business-rule consistency
        # ---------------------------------------------------------------
        _zero_ok(cur, "status:cancelled_has_cancelled_at",
                 "SELECT count(*) FROM erp.sales_orders WHERE status='cancelled' AND cancelled_at IS NULL")
        _zero_ok(cur, "status:non_cancelled_has_no_cancelled_at",
                 "SELECT count(*) FROM erp.sales_orders WHERE status<>'cancelled' AND cancelled_at IS NOT NULL")
        _zero_ok(cur, "status:delivered_has_delivered_shipment",
                 """
                 SELECT count(*) FROM erp.sales_orders o
                 WHERE o.status='delivered'
                   AND NOT EXISTS (SELECT 1 FROM erp.shipments s
                                   WHERE s.sales_order_id=o.sales_order_id
                                     AND s.status='delivered')
                 """)
        _zero_ok(cur, "status:received_po_has_receipt",
                 """
                 SELECT count(*) FROM erp.purchase_orders o
                 WHERE o.status IN ('received','partially_received')
                   AND NOT EXISTS (SELECT 1 FROM erp.goods_receipts g
                                   WHERE g.purchase_order_id=o.purchase_order_id)
                 """)
        _zero_ok(cur, "status:pending_approval_has_no_receipt",
                 """
                 SELECT count(*) FROM erp.purchase_orders o
                 WHERE o.status='pending_approval'
                   AND EXISTS (SELECT 1 FROM erp.goods_receipts g
                               WHERE g.purchase_order_id=o.purchase_order_id)
                 """)
        _zero_ok(cur, "returns:restock_items_traceable",
                 """
                 SELECT count(*) FROM erp.return_items ri
                 WHERE ri.disposition='restock'
                   AND NOT EXISTS (SELECT 1 FROM erp.stock_movements m
                                   WHERE m.reference_type='return'
                                     AND m.reference_id=ri.return_id
                                     AND m.product_id=ri.product_id
                                     AND m.movement_type='customer_return')
                 """)

        # ---------------------------------------------------------------
        # 9. Chronological consistency
        # ---------------------------------------------------------------
        _zero_ok(cur, "chrono:shipment_delivered_after_shipped",
                 "SELECT count(*) FROM erp.shipments "
                 "WHERE delivered_date IS NOT NULL AND shipped_date IS NOT NULL "
                 "AND delivered_date < shipped_date")
        _zero_ok(cur, "chrono:transfer_received_after_dispatched",
                 "SELECT count(*) FROM erp.stock_transfers "
                 "WHERE received_date IS NOT NULL AND dispatched_date IS NOT NULL "
                 "AND received_date < dispatched_date")
        _zero_ok(cur, "chrono:transfer_dispatched_after_request",
                 "SELECT count(*) FROM erp.stock_transfers "
                 "WHERE dispatched_date IS NOT NULL AND dispatched_date < request_date")
        _zero_ok(cur, "chrono:goods_receipt_after_po_order",
                 """
                 SELECT count(*) FROM erp.goods_receipts g
                 JOIN erp.purchase_orders o ON o.purchase_order_id=g.purchase_order_id
                 WHERE g.received_date < o.order_date
                 """)
        _zero_ok(cur, "chrono:return_after_order",
                 """
                 SELECT count(*) FROM erp.returns r
                 JOIN erp.sales_orders o ON o.sales_order_id=r.sales_order_id
                 WHERE r.return_date < o.order_date
                 """)
        _zero_ok(cur, "chrono:payment_after_order",
                 """
                 SELECT count(*) FROM erp.payments p
                 JOIN erp.sales_orders o ON o.sales_order_id=p.sales_order_id
                 WHERE p.payment_date < o.order_date
                 """)

        # ---------------------------------------------------------------
        # 10. Association & scenario coverage (informational)
        # ---------------------------------------------------------------
        _count_ok(cur, "assoc:every_order_has_items",
                  "SELECT count(*) FROM erp.sales_orders o "
                  "WHERE NOT EXISTS (SELECT 1 FROM erp.sales_order_items i "
                  "WHERE i.sales_order_id=o.sales_order_id)", maximum=0)
        _count_ok(cur, "assoc:every_inventory_row_has_wh",
                  "SELECT count(*) FROM erp.inventory i "
                  "JOIN erp.warehouses w ON w.warehouse_id=i.warehouse_id",
                  minimum=1, essential=False)

        out_of_stock = _scalar(cur, "SELECT count(*) FROM erp.inventory WHERE quantity_on_hand = 0")
        low_stock = _scalar(cur, "SELECT count(*) FROM erp.inventory "
                                 "WHERE quantity_on_hand > 0 AND quantity_on_hand <= reorder_point")
        surplus = _scalar(cur, "SELECT count(*) FROM erp.inventory "
                               "WHERE quantity_on_hand > reorder_point * 3")
        stockout_orders = _scalar(cur, "SELECT count(*) FROM erp.sales_orders WHERE status='pending'")
        delayed_ship = _scalar(cur, "SELECT count(*) FROM erp.shipments "
                                    "WHERE delivered_date IS NOT NULL AND delivered_date::date > promised_date")
        RESULTS.append(Check("scenario:out_of_stock_products", True,
                             f"{out_of_stock} (warehouse,product) pairs at zero stock", essential=False))
        RESULTS.append(Check("scenario:low_stock_products", True,
                             f"{low_stock} pairs at/under reorder point", essential=False))
        RESULTS.append(Check("scenario:surplus_inventory", True,
                             f"{surplus} pairs with excess inventory", essential=False))
        RESULTS.append(Check("scenario:unfulfilled_orders", True,
                             f"{stockout_orders} orders unfulfilled (pending)", essential=False))
        RESULTS.append(Check("scenario:delayed_shipments", True,
                             f"{delayed_ship} deliveries later than promised", essential=False))

    return _report()


def _report() -> bool:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    essential_fail = [c for c in RESULTS if c.essential and not c.passed]
    optional_fail = [c for c in RESULTS if not c.essential and not c.passed]

    lines = []
    lines.append("=" * 78)
    lines.append("SYNTHETIC ERP DATASET - VALIDATION REPORT")
    lines.append(f"generated at {ts}  |  seed={CONFIG.seed}")
    lines.append("=" * 78)
    lines.append(f"{'CHECK':<52}{'RESULT':<10}")
    lines.append("-" * 78)
    for c in RESULTS:
        status = "PASS" if c.passed else "FAIL"
        tag = "" if c.essential else " (info)"
        lines.append(f"{c.name[:50]:<52}{status}{tag}")
        if not c.passed or c.name.startswith(("rowcount", "scenario")):
            lines.append(f"    -> {c.detail}")
    lines.append("-" * 78)
    n_pass = sum(1 for c in RESULTS if c.passed)
    lines.append(f"passed: {n_pass}/{len(RESULTS)}   "
                 f"essential failures: {len(essential_fail)}   "
                 f"info failures: {len(optional_fail)}")
    valid = len(essential_fail) == 0
    lines.append(f"DATASET VALID: {'YES' if valid else 'NO'}")
    lines.append("=" * 78)

    text = "\n".join(lines)
    print(text)

    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    (out / "validation_report.txt").write_text(text + "\n", encoding="utf-8")

    return valid


if __name__ == "__main__":
    sys.exit(0 if run_validation() else 1)

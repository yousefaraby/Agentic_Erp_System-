"""Load the generated synthetic dataset into PostgreSQL.

Usage
-----
    python load_data.py                 # generate, (re)create schema, load, validate
    python load_data.py --no-validate   # skip the validation report
    python load_data.py --generate-only # write the dataset to ./out_repaired as CSV, no DB

The pipeline builds every row in memory in dependency order, runs the inventory
ledger simulation, then bulk-loads with COPY inside a single transaction.
Related inventory updates therefore either all succeed or all roll back.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

from config import CONFIG
from db import create_schema, get_connection
from generators import (
    customers,
    inventory,
    organizational,
    procurement,
    products,
    sales,
    suppliers,
)
from generators.base import World

# Dependency order: parents before children.
TABLE_ORDER = [
    "roles", "permissions", "role_permissions",
    "branches", "warehouses", "branch_warehouse_services", "departments", "employees", "users",
    "product_categories", "products",
    "suppliers", "supplier_products",
    "customers",
    "sales_orders", "sales_order_items", "payments", "shipments",
    "returns", "return_items", "payment_refunds",
    "inventory", "inventory_reservations", "inventory_reservation_items",
    "stock_movements", "stock_transfers", "stock_transfer_items",
    "purchase_orders", "purchase_order_items", "goods_receipts", "goods_receipt_items",
]

TABLE_COLUMNS: dict[str, list[str]] = {
    "roles": ["role_id", "role_name", "description", "created_at"],
    "permissions": ["permission_id", "permission_key", "description"],
    "role_permissions": ["role_id", "permission_id"],
    "branches": ["branch_id", "branch_code", "name", "branch_type", "address_line",
                 "city", "region", "country", "phone", "email", "opened_date",
                 "is_active", "created_at", "updated_at"],
    "warehouses": ["warehouse_id", "warehouse_code", "name", "warehouse_type",
                   "branch_id", "address_line", "city", "region", "country",
                   "capacity_units", "is_active", "created_at", "updated_at"],
    "branch_warehouse_services": ["branch_id", "warehouse_id", "is_primary",
                                   "can_fulfill", "can_transfer", "valid_from",
                                   "valid_to", "created_at"],
    "departments": ["department_id", "department_code", "name", "created_at"],
    "employees": ["employee_id", "employee_code", "branch_id", "warehouse_id",
                  "first_name", "last_name", "email", "phone", "job_title",
                  "department_id", "hire_date", "termination_date", "manager_id",
                  "is_active", "created_at", "updated_at"],
    "users": ["user_id", "username", "email", "password_hash", "employee_id",
              "role_id", "is_active", "last_login_at", "created_at", "updated_at"],
    "product_categories": ["category_id", "category_code", "name",
                           "parent_category_id", "description", "is_active",
                           "created_at", "updated_at"],
    "products": ["product_id", "sku", "name", "category_id", "description",
                 "unit_of_measure", "cost_price", "list_price", "reorder_point",
                 "reorder_quantity", "safety_stock", "weight_kg", "is_active",
                 "created_at", "updated_at"],
    "suppliers": ["supplier_id", "supplier_code", "name", "contact_name", "email",
                  "phone", "address_line", "city", "country", "payment_terms",
                  "default_lead_time_days", "rating", "is_active", "created_at",
                  "updated_at"],
    "supplier_products": ["supplier_product_id", "supplier_id", "product_id",
                          "supplier_sku", "unit_cost", "lead_time_days",
                          "min_order_qty", "is_preferred", "valid_from", "valid_to",
                          "created_at"],
    "customers": ["customer_id", "customer_code", "customer_type", "first_name",
                  "last_name", "company_name", "email", "phone", "address_line",
                  "city", "region", "country", "loyalty_tier", "credit_limit",
                  "registration_date", "is_active", "created_at", "updated_at"],
    "sales_orders": ["sales_order_id", "order_number", "branch_id", "customer_id",
                     "employee_id", "order_date", "status", "channel", "subtotal",
                     "discount_amount", "tax_amount", "shipping_amount",
                     "total_amount", "currency", "notes", "cancelled_at",
                     "created_at", "updated_at"],
    "sales_order_items": ["sales_order_item_id", "sales_order_id", "line_number",
                          "product_id", "warehouse_id", "quantity",
                          "quantity_fulfilled", "unit_price", "discount_rate",
                          "tax_rate", "line_subtotal", "line_total", "created_at"],
    "payments": ["payment_id", "payment_number", "sales_order_id", "payment_date",
                 "amount", "method", "status", "reference", "created_at"],
    "shipments": ["shipment_id", "shipment_number", "sales_order_id", "warehouse_id",
                  "carrier", "tracking_number", "shipped_date", "promised_date",
                  "delivered_date", "status", "shipping_cost", "created_at",
                  "updated_at"],
    "returns": ["return_id", "return_number", "sales_order_id", "customer_id",
                "warehouse_id", "return_date", "reason", "status", "refund_amount",
                "created_at", "updated_at"],
    "return_items": ["return_item_id", "return_id", "sales_order_item_id",
                     "product_id", "quantity", "unit_price", "line_refund",
                     "item_condition", "disposition", "created_at"],
    "payment_refunds": ["refund_id", "refund_number", "payment_id", "return_id",
                        "refund_date", "amount", "status", "created_at"],
    "inventory": ["inventory_id", "warehouse_id", "product_id", "quantity_on_hand",
                  "quantity_reserved", "reorder_point", "last_counted_at",
                  "updated_at"],
    "inventory_reservations": ["reservation_id", "sales_order_id", "warehouse_id",
                               "status", "reserved_at", "expires_at", "created_at",
                               "updated_at"],
    "inventory_reservation_items": ["reservation_item_id", "reservation_id",
                                    "sales_order_item_id", "warehouse_id", "product_id",
                                    "quantity", "created_at"],
    "stock_movements": ["movement_id", "warehouse_id", "product_id", "movement_type",
                        "quantity_change", "balance_after", "reference_type",
                        "reference_id", "unit_cost", "movement_date", "notes",
                        "created_at"],
    "stock_transfers": ["transfer_id", "transfer_number", "source_warehouse_id",
                        "destination_warehouse_id", "requested_by", "approved_by",
                        "request_date", "status", "dispatched_date", "received_date",
                        "expected_date", "total_quantity", "reason", "notes",
                        "created_at", "updated_at"],
    "stock_transfer_items": ["transfer_item_id", "transfer_id", "product_id",
                             "quantity_requested", "quantity_shipped",
                             "quantity_received", "unit_cost", "created_at"],
    "purchase_orders": ["purchase_order_id", "po_number", "supplier_id",
                        "warehouse_id", "requested_by", "approved_by", "status",
                        "order_date", "expected_date", "subtotal", "tax_amount",
                        "shipping_amount", "total_amount", "currency", "notes",
                        "created_at", "updated_at"],
    "purchase_order_items": ["po_item_id", "purchase_order_id", "line_number",
                             "product_id", "quantity_ordered", "quantity_received",
                             "unit_cost", "line_total", "created_at"],
    "goods_receipts": ["receipt_id", "receipt_number", "purchase_order_id",
                       "warehouse_id", "received_by", "received_date", "status",
                       "notes", "created_at"],
    "goods_receipt_items": ["receipt_item_id", "receipt_id", "po_item_id",
                            "product_id", "quantity_received", "unit_cost",
                            "line_total", "created_at"],
}


def build_world() -> World:
    """Run the full generation pipeline in dependency order."""
    cfg = CONFIG
    cfg.validate()
    world = World(cfg, cfg.seed)

    organizational.generate(world)
    products.generate(world)
    suppliers.generate(world)
    customers.generate(world)

    inventory.init_assortment(world)      # which products each warehouse carries
    sales.generate_demand(world)          # orders + items; records demand
    procurement.generate(world)           # POs, receipts sized to demand
    inventory.generate_transfers(world)   # inter-warehouse transfer requests
    inventory.run_ledger(world)           # simulate stock, finalise fulfilment

    return world


def _row_tuple(row: dict, columns: list[str]) -> tuple:
    return tuple(row.get(col) for col in columns)


def load(world: World) -> None:
    create_schema()
    total = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in TABLE_ORDER:
                rows = world.rows.get(table, [])
                cols = TABLE_COLUMNS[table]
                if not rows:
                    print(f"  {table:<24} 0")
                    continue
                collist = ", ".join(cols)
                stmt = f"COPY erp.{table} ({collist}) FROM STDIN"
                with cur.copy(stmt) as copy:
                    for row in rows:
                        copy.write_row(_row_tuple(row, cols))
                total += len(rows)
                print(f"  {table:<24} {len(rows)}")
    print(f"  {'TOTAL':<24} {total}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and load the synthetic ERP dataset")
    parser.add_argument("--no-validate", action="store_true",
                        help="skip running the validation report")
    parser.add_argument("--generate-only", action="store_true",
                        help="write CSV files to ./out_repaired instead of loading PostgreSQL")
    parser.add_argument("--output-dir", type=Path, default=Path("out_repaired"),
                        help="CSV output directory for --generate-only (default: out_repaired)")
    args = parser.parse_args(argv)

    t0 = time.time()
    print(f"Generating dataset (seed={CONFIG.seed}, orders={CONFIG.n_sales_orders}, "
          f"products={CONFIG.n_products})...")
    world = build_world()
    print(f"Generation complete in {time.time() - t0:.1f}s")

    if args.generate_only:
        out = args.output_dir
        if not out.is_absolute():
            out = Path(__file__).resolve().parent / out
        out.mkdir(exist_ok=True)
        for table in TABLE_ORDER:
            rows = world.rows.get(table, [])
            cols = TABLE_COLUMNS[table]
            with (out / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(cols)
                for row in rows:
                    writer.writerow(_row_tuple(row, cols))
        manifest = {
            "seed": CONFIG.seed,
            "history_start": CONFIG.history_start.isoformat(),
            "history_end": CONFIG.history_end.isoformat(),
            "table_counts": {table: len(world.rows.get(table, [])) for table in TABLE_ORDER},
            "sha256": {
                f"{table}.csv": hashlib.sha256((out / f"{table}.csv").read_bytes()).hexdigest()
                for table in TABLE_ORDER
            },
        }
        (out / "data_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Wrote CSV files to {out}")
        return 0

    print("Loading into PostgreSQL...")
    load(world)

    if not args.no_validate:
        print("\nRunning validation...")
        from validate_data import run_validation
        ok = run_validation()
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

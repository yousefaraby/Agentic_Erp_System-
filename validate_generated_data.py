"""Validate generated CSVs without connecting to or modifying PostgreSQL."""
from __future__ import annotations

import csv
import argparse
import hashlib
import sys
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from load_data import TABLE_COLUMNS, TABLE_ORDER

CHECKS: list[tuple[str, int, str]] = []
_parser = argparse.ArgumentParser(description="Validate generated ERP CSV files")
_parser.add_argument("--directory", type=Path, default=Path("out_repaired"))
_args = _parser.parse_args()
OUT = _args.directory if _args.directory.is_absolute() else Path(__file__).resolve().parent / _args.directory


def check(name: str, failures: list, detail: str = "") -> None:
    CHECKS.append((name, len(failures), detail or (", ".join(map(str, failures[:5])) if failures else "")))


def read_table(table: str) -> list[dict[str, str]]:
    path = OUT / f"{table}.csv"
    if not path.exists():
        check(f"file:{table}", ["missing"])
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        check(f"columns:{table}", [] if reader.fieldnames == TABLE_COLUMNS[table] else [reader.fieldnames],
              f"expected {TABLE_COLUMNS[table]}")
        check(f"rows:{table}", [] if rows else ["empty"], f"{len(rows)} rows")
        return rows


def dec(value: str | None) -> Decimal:
    return Decimal(value or "0")


TABLES = {name: read_table(name) for name in TABLE_ORDER}

schema_text = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
schema_tables = {name for name in re.findall(r"CREATE\s+TABLE\s+(\w+)\s*\(", schema_text, re.I)}
check("schema:tables_match_export_tables",
      sorted(schema_tables.symmetric_difference(TABLE_ORDER)))
schema_column_errors = []
for match in re.finditer(r"CREATE\s+TABLE\s+(\w+)\s*\((.*?)\n\);", schema_text, re.I | re.S):
    table, body = match.groups()
    columns = []
    for line in body.splitlines():
        found = re.match(r"\s*([a-zA-Z_]\w*)\s+", line)
        if found and found.group(1).upper() not in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
            columns.append(found.group(1))
    if table in TABLE_COLUMNS and columns != TABLE_COLUMNS[table]:
        schema_column_errors.append(f"{table}: {columns}")
check("schema:columns_match_export_columns", schema_column_errors)

manifest_path = OUT / "data_manifest.json"
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_errors = []
    for table in TABLE_ORDER:
        path = OUT / f"{table}.csv"
        if not path.exists():
            manifest_errors.append(f"{table}: missing file")
            continue
        if manifest.get("table_counts", {}).get(table) != len(TABLES[table]):
            manifest_errors.append(f"{table}: count")
        if manifest.get("sha256", {}).get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
            manifest_errors.append(f"{table}: hash")
    check("manifest:counts_and_hashes", manifest_errors)
else:
    check("manifest:present", ["data_manifest.json missing"])

# Primary and unique keys declared by the schema.
KEYS = {
    "branches": [("branch_id",), ("branch_code",)],
    "warehouses": [("warehouse_id",), ("warehouse_code",)],
    "branch_warehouse_services": [("branch_id", "warehouse_id", "valid_from")],
    "departments": [("department_id",), ("department_code",), ("name",)],
    "employees": [("employee_id",), ("employee_code",), ("email",)],
    "roles": [("role_id",), ("role_name",)],
    "permissions": [("permission_id",), ("permission_key",)],
    "role_permissions": [("role_id", "permission_id")],
    "users": [("user_id",), ("username",), ("email",)],
    "product_categories": [("category_id",), ("category_code",)],
    "products": [("product_id",), ("sku",)],
    "suppliers": [("supplier_id",), ("supplier_code",), ("email",)],
    "supplier_products": [("supplier_product_id",), ("supplier_id", "product_id")],
    "customers": [("customer_id",), ("customer_code",), ("email",)],
    "sales_orders": [("sales_order_id",), ("order_number",)],
    "sales_order_items": [("sales_order_item_id",), ("sales_order_id", "line_number")],
    "payments": [("payment_id",), ("payment_number",)],
    "shipments": [("shipment_id",), ("shipment_number",)],
    "returns": [("return_id",), ("return_number",)],
    "return_items": [("return_item_id",)],
    "payment_refunds": [("refund_id",), ("refund_number",)],
    "inventory": [("inventory_id",), ("warehouse_id", "product_id")],
    "inventory_reservations": [("reservation_id",), ("sales_order_id",)],
    "inventory_reservation_items": [("reservation_item_id",), ("reservation_id", "sales_order_item_id")],
    "stock_movements": [("movement_id",)],
    "stock_transfers": [("transfer_id",), ("transfer_number",)],
    "stock_transfer_items": [("transfer_item_id",)],
    "purchase_orders": [("purchase_order_id",), ("po_number",)],
    "purchase_order_items": [("po_item_id",), ("purchase_order_id", "line_number")],
    "goods_receipts": [("receipt_id",), ("receipt_number",)],
    "goods_receipt_items": [("receipt_item_id",)],
}

for table, keysets in KEYS.items():
    rows = TABLES[table]
    for key in keysets:
        counts = Counter(tuple(row[col] for col in key) for row in rows)
        dups = [val for val, n in counts.items() if n > 1]
        check(f"unique:{table}.{'+'.join(key)}", dups)

# Foreign keys declared in schema.sql.
FKS = {
    "warehouses": {"branch_id": ("branches", "branch_id")},
    "branch_warehouse_services": {"branch_id": ("branches", "branch_id"), "warehouse_id": ("warehouses", "warehouse_id")},
    "employees": {"branch_id": ("branches", "branch_id"), "warehouse_id": ("warehouses", "warehouse_id"), "department_id": ("departments", "department_id"), "manager_id": ("employees", "employee_id")},
    "users": {"employee_id": ("employees", "employee_id"), "role_id": ("roles", "role_id")},
    "role_permissions": {"role_id": ("roles", "role_id"), "permission_id": ("permissions", "permission_id")},
    "product_categories": {"parent_category_id": ("product_categories", "category_id")},
    "products": {"category_id": ("product_categories", "category_id")},
    "supplier_products": {"supplier_id": ("suppliers", "supplier_id"), "product_id": ("products", "product_id")},
    "sales_orders": {"branch_id": ("branches", "branch_id"), "customer_id": ("customers", "customer_id"), "employee_id": ("employees", "employee_id")},
    "sales_order_items": {"sales_order_id": ("sales_orders", "sales_order_id"), "product_id": ("products", "product_id"), "warehouse_id": ("warehouses", "warehouse_id")},
    "payments": {"sales_order_id": ("sales_orders", "sales_order_id")},
    "shipments": {"sales_order_id": ("sales_orders", "sales_order_id"), "warehouse_id": ("warehouses", "warehouse_id")},
    "returns": {"sales_order_id": ("sales_orders", "sales_order_id"), "customer_id": ("customers", "customer_id"), "warehouse_id": ("warehouses", "warehouse_id")},
    "return_items": {"return_id": ("returns", "return_id"), "sales_order_item_id": ("sales_order_items", "sales_order_item_id"), "product_id": ("products", "product_id")},
    "payment_refunds": {"payment_id": ("payments", "payment_id"), "return_id": ("returns", "return_id")},
    "inventory": {"warehouse_id": ("warehouses", "warehouse_id"), "product_id": ("products", "product_id")},
    "inventory_reservations": {"sales_order_id": ("sales_orders", "sales_order_id"), "warehouse_id": ("warehouses", "warehouse_id")},
    "inventory_reservation_items": {"reservation_id": ("inventory_reservations", "reservation_id"), "sales_order_item_id": ("sales_order_items", "sales_order_item_id"), "warehouse_id": ("warehouses", "warehouse_id"), "product_id": ("products", "product_id")},
    "stock_movements": {"warehouse_id": ("warehouses", "warehouse_id"), "product_id": ("products", "product_id")},
    "stock_transfers": {"source_warehouse_id": ("warehouses", "warehouse_id"), "destination_warehouse_id": ("warehouses", "warehouse_id"), "requested_by": ("employees", "employee_id"), "approved_by": ("employees", "employee_id")},
    "stock_transfer_items": {"transfer_id": ("stock_transfers", "transfer_id"), "product_id": ("products", "product_id")},
    "purchase_orders": {"supplier_id": ("suppliers", "supplier_id"), "warehouse_id": ("warehouses", "warehouse_id"), "requested_by": ("employees", "employee_id"), "approved_by": ("employees", "employee_id")},
    "purchase_order_items": {"purchase_order_id": ("purchase_orders", "purchase_order_id"), "product_id": ("products", "product_id")},
    "goods_receipts": {"purchase_order_id": ("purchase_orders", "purchase_order_id"), "warehouse_id": ("warehouses", "warehouse_id"), "received_by": ("employees", "employee_id")},
    "goods_receipt_items": {"receipt_id": ("goods_receipts", "receipt_id"), "po_item_id": ("purchase_order_items", "po_item_id"), "product_id": ("products", "product_id")},
}
for child, mapping in FKS.items():
    for col, (parent, parent_col) in mapping.items():
        valid = {r[parent_col] for r in TABLES[parent]}
        orphans = [r.get(col) for r in TABLES[child] if r.get(col) and r[col] not in valid]
        check(f"fk:{child}.{col}", orphans)

by_id = {table: {row[next(k[0] for k in KEYS[table] if len(k) == 1)]: row
                 for row in TABLES[table]}
         for table in TABLES if any(len(k) == 1 for k in KEYS[table])}
branches = by_id["branches"]
warehouses = by_id["warehouses"]
employees = by_id["employees"]
orders = by_id["sales_orders"]
services = {(r["branch_id"], r["warehouse_id"]): r for r in TABLES["branch_warehouse_services"]}

primary_counts = Counter(r["branch_id"] for r in TABLES["branch_warehouse_services"]
                         if r["is_primary"].lower() == "true" and not r["valid_to"])
check("org:one_active_primary_warehouse_per_branch",
      [bid for bid in branches if primary_counts[bid] != 1])
check("org:every_branch_has_fulfilment_warehouse",
      [bid for bid in branches if not any(r["branch_id"] == bid and r["can_fulfill"].lower() == "true"
                                           and not r["valid_to"] for r in TABLES["branch_warehouse_services"])])
check("sales:employee_matches_order_branch",
      [o["sales_order_id"] for o in TABLES["sales_orders"]
       if o["employee_id"] and employees[o["employee_id"]]["branch_id"] != o["branch_id"]])
check("sales:warehouse_serves_order_branch",
      [i["sales_order_item_id"] for i in TABLES["sales_order_items"]
       if (orders[i["sales_order_id"]]["branch_id"], i["warehouse_id"]) not in services
       or services[(orders[i["sales_order_id"]]["branch_id"], i["warehouse_id"])]["can_fulfill"].lower() != "true"])
check("org:employee_warehouse_is_in_branch_service_map",
      [e["employee_id"] for e in TABLES["employees"] if e["warehouse_id"]
       and (e["branch_id"], e["warehouse_id"]) not in services])

dept_names = {r["department_id"]: r["name"] for r in TABLES["departments"]}
check("procurement:requester_and_approver_roles",
      [p["purchase_order_id"] for p in TABLES["purchase_orders"]
       if (p["requested_by"] and dept_names[employees[p["requested_by"]]["department_id"]] != "Procurement")
       or (p["approved_by"] and dept_names[employees[p["approved_by"]]["department_id"]]
           not in ("Management", "Finance"))
       or (p["approved_by"] and p["approved_by"] == p["requested_by"])])
check("transfer:requester_and_approver_separation",
      [t["transfer_id"] for t in TABLES["stock_transfers"]
       if (t["approved_by"] and t["approved_by"] == t["requested_by"])
       or (t["requested_by"] and dept_names[employees[t["requested_by"]]["department_id"]] != "Warehouse")
       or (t["approved_by"] and dept_names[employees[t["approved_by"]]["department_id"]]
           not in ("Management", "Warehouse"))])

items_by_order: dict[str, list[dict]] = defaultdict(list)
for item in TABLES["sales_order_items"]:
    items_by_order[item["sales_order_id"]].append(item)
money_errors = []
for order in TABLES["sales_orders"]:
    items = items_by_order[order["sales_order_id"]]
    subtotal = sum((dec(i["line_subtotal"]) for i in items), Decimal(0))
    discount = sum(((dec(i["line_subtotal"]) * dec(i["discount_rate"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for i in items), Decimal(0))
    tax = sum((((dec(i["line_subtotal"]) - (dec(i["line_subtotal"]) * dec(i["discount_rate"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) * dec(i["tax_rate"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for i in items), Decimal(0))
    if subtotal != dec(order["subtotal"]) or discount != dec(order["discount_amount"]) or tax != dec(order["tax_amount"]):
        money_errors.append(order["sales_order_id"])
    if dec(order["total_amount"]) != subtotal - discount + tax + dec(order["shipping_amount"]):
        money_errors.append(order["sales_order_id"])
check("sales:header_totals_reconcile_to_lines", money_errors)
check("sales:fulfilled_not_over_ordered",
      [i["sales_order_item_id"] for i in TABLES["sales_order_items"] if int(i["quantity_fulfilled"]) > int(i["quantity"])])
check("sales:all_orders_have_lines", [o["sales_order_id"] for o in TABLES["sales_orders"] if not items_by_order[o["sales_order_id"]]])
paid_by_order = Counter()
for payment in TABLES["payments"]:
    paid_by_order[payment["sales_order_id"]] += dec(payment["amount"])
check("sales:payment_value_not_above_order_total",
      [o["sales_order_id"] for o in TABLES["sales_orders"]
       if paid_by_order[o["sales_order_id"]] > dec(o["total_amount"])])
check("sales:completed_payment_has_delivered_order",
      [p["payment_id"] for p in TABLES["payments"]
       if p["status"] == "completed" and orders[p["sales_order_id"]]["status"] != "delivered"])

po_by_id = by_id["purchase_orders"]
po_items = by_id["purchase_order_items"]
receipt_by_id = by_id["goods_receipts"]
receipt_errors = []
received_by_line = Counter()
for row in TABLES["goods_receipt_items"]:
    line = po_items[row["po_item_id"]]
    receipt = receipt_by_id[row["receipt_id"]]
    po = po_by_id[line["purchase_order_id"]]
    received_by_line[row["po_item_id"]] += int(row["quantity_received"])
    if row["product_id"] != line["product_id"] or receipt["warehouse_id"] != po["warehouse_id"]:
        receipt_errors.append(row["receipt_item_id"])
check("procurement:receipt_links_match_po", receipt_errors)
check("procurement:po_received_qty_matches_receipts",
      [line["po_item_id"] for line in TABLES["purchase_order_items"]
       if int(line["quantity_received"]) != received_by_line[line["po_item_id"]]])
check("procurement:receipt_receiver_is_warehouse_staff",
      [r["receipt_id"] for r in TABLES["goods_receipts"] if r["received_by"]
       and (dept_names[employees[r["received_by"]]["department_id"]] != "Warehouse"
            or employees[r["received_by"]]["warehouse_id"] != r["warehouse_id"])])

reservations = by_id["inventory_reservations"]
order_lines = by_id["sales_order_items"]
reservation_errors = []
reserved_by_pair = Counter()
reserved_by_order = defaultdict(list)
for line in TABLES["inventory_reservation_items"]:
    reservation = reservations[line["reservation_id"]]
    order_line = order_lines[line["sales_order_item_id"]]
    reserved_by_order[reservation["sales_order_id"]].append(line)
    if (line["warehouse_id"] != reservation["warehouse_id"]
            or line["warehouse_id"] != order_line["warehouse_id"]
            or line["product_id"] != order_line["product_id"]
            or int(line["quantity"]) != int(order_line["quantity"])):
        reservation_errors.append(line["reservation_item_id"])
    if reservation["status"] == "active":
        reserved_by_pair[(line["warehouse_id"], line["product_id"])] += int(line["quantity"])
check("inventory:reservation_lines_match_open_order_lines", reservation_errors)
check("inventory:active_reservations_match_order_status",
      [r["reservation_id"] for r in TABLES["inventory_reservations"]
       if (orders[r["sales_order_id"]]["status"] == "reserved") != (r["status"] == "active")])
check("inventory:every_reserved_order_has_reservation",
      [o["sales_order_id"] for o in TABLES["sales_orders"]
       if o["status"] == "reserved" and o["sales_order_id"] not in reserved_by_order])
check("inventory:reserved_projection_matches_active_reservations",
      [(i["warehouse_id"], i["product_id"]) for i in TABLES["inventory"]
       if int(i["quantity_reserved"]) != reserved_by_pair[
           (i["warehouse_id"], i["product_id"])
       ]])

returns = by_id["returns"]
payments = by_id["payments"]
refund_amounts = Counter()
for refund in TABLES["payment_refunds"]:
    refund_amounts[refund["return_id"]] += dec(refund["amount"])
return_line_amounts = Counter()
for line in TABLES["return_items"]:
    return_line_amounts[line["return_id"]] += dec(line["line_refund"])
check("returns:refunds_reconcile_to_return_and_items",
      [rid for rid, ret in returns.items()
       if refund_amounts[rid] != dec(ret["refund_amount"])
       or return_line_amounts[rid] != dec(ret["refund_amount"])])
check("returns:refund_does_not_exceed_payment",
      [refund["refund_id"] for refund in TABLES["payment_refunds"]
       if dec(refund["amount"]) > dec(payments[refund["payment_id"]]["amount"])])
check("returns:refund_payment_matches_order",
      [refund["refund_id"] for refund in TABLES["payment_refunds"]
       if payments[refund["payment_id"]]["sales_order_id"] != returns[refund["return_id"]]["sales_order_id"]])
returned_by_line = Counter()
for line in TABLES["return_items"]:
    returned_by_line[line["sales_order_item_id"]] += int(line["quantity"])
check("returns:quantity_not_above_fulfilled",
      [line_id for line_id, qty in returned_by_line.items()
       if qty > int(order_lines[line_id]["quantity_fulfilled"])])

movement_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
for row in TABLES["stock_movements"]:
    movement_groups[(row["warehouse_id"], row["product_id"])].append(row)
ledger_errors = []
for key, movements in movement_groups.items():
    movements.sort(key=lambda m: (m["movement_date"], int(m["movement_id"])))
    prev = None
    for movement in movements:
        expected = int(movement["quantity_change"]) if prev is None else prev + int(movement["quantity_change"])
        if int(movement["balance_after"]) != expected or expected < 0:
            ledger_errors.append((key, movement["movement_id"]))
        prev = int(movement["balance_after"])
stock_by_pair = {(r["warehouse_id"], r["product_id"]): int(r["quantity_on_hand"]) for r in TABLES["inventory"]}
check("inventory:movement_balance_sequence", ledger_errors)
check("inventory:current_stock_matches_ledger",
      [pair for pair, qty in stock_by_pair.items()
       if qty != sum(int(m["quantity_change"]) for m in movement_groups[pair])])
product_by_id = by_id["products"]
check("inventory:retail_movements_use_product_cost",
      [m["movement_id"] for m in TABLES["stock_movements"]
       if m["movement_type"] in ("initial", "sale", "customer_return", "transfer_out", "transfer_in")
       and dec(m["unit_cost"]) != dec(product_by_id[m["product_id"]]["cost_price"])])

reference_tables = {
    "sales_order": "sales_orders",
    "goods_receipt": "goods_receipts",
    "stock_transfer": "stock_transfers",
    "return": "returns",
}
reference_errors = []
for movement in TABLES["stock_movements"]:
    if movement["movement_type"] == "initial":
        if movement["reference_type"] != "manual" or movement["reference_id"]:
            reference_errors.append(movement["movement_id"])
        continue
    parent_table = reference_tables.get(movement["reference_type"])
    if (not movement["reference_id"] or not parent_table
            or movement["reference_id"] not in by_id[parent_table]):
        reference_errors.append(movement["movement_id"])
check("inventory:movements_trace_to_source_transaction", reference_errors)

def aggregate(rows: list[dict], key_fn, value_fn) -> Counter:
    result = Counter()
    for row in rows:
        result[key_fn(row)] += value_fn(row)
    return result

movement_sales = aggregate(
    [m for m in TABLES["stock_movements"] if m["movement_type"] == "sale"],
    lambda m: (m["reference_id"], m["warehouse_id"], m["product_id"]),
    lambda m: -int(m["quantity_change"]),
)
expected_sales = aggregate(
    [i for i in TABLES["sales_order_items"] if int(i["quantity_fulfilled"]) > 0],
    lambda i: (i["sales_order_id"], i["warehouse_id"], i["product_id"]),
    lambda i: int(i["quantity_fulfilled"]),
)
check("inventory:sale_movements_match_fulfilled_quantities",
      [key for key in set(movement_sales) | set(expected_sales)
       if movement_sales[key] != expected_sales[key]])

movement_receipts = aggregate(
    [m for m in TABLES["stock_movements"] if m["movement_type"] == "purchase_receipt"],
    lambda m: (m["reference_id"], m["warehouse_id"], m["product_id"]),
    lambda m: int(m["quantity_change"]),
)
expected_receipts = aggregate(
    TABLES["goods_receipt_items"],
    lambda i: (receipt_by_id[i["receipt_id"]]["receipt_id"],
               receipt_by_id[i["receipt_id"]]["warehouse_id"], i["product_id"]),
    lambda i: int(i["quantity_received"]),
)
check("inventory:receipt_movements_match_received_quantities",
      [key for key in set(movement_receipts) | set(expected_receipts)
       if movement_receipts[key] != expected_receipts[key]])

transfer_items_by_id = defaultdict(list)
for item in TABLES["stock_transfer_items"]:
    transfer_items_by_id[item["transfer_id"]].append(item)
transfer_total_errors = [t["transfer_id"] for t in TABLES["stock_transfers"]
                         if sum(int(i["quantity_requested"]) for i in transfer_items_by_id[t["transfer_id"]])
                         != int(t["total_quantity"])]
transfer_qty_errors = [i["transfer_item_id"] for i in TABLES["stock_transfer_items"]
                       if int(i["quantity_shipped"]) > int(i["quantity_requested"])
                       or int(i["quantity_received"]) > int(i["quantity_shipped"])]
check("transfers:header_and_line_quantities", transfer_total_errors + transfer_qty_errors)
transfer_out = aggregate(
    [m for m in TABLES["stock_movements"] if m["movement_type"] == "transfer_out"],
    lambda m: (m["reference_id"], m["warehouse_id"], m["product_id"]),
    lambda m: -int(m["quantity_change"]),
)
transfer_in = aggregate(
    [m for m in TABLES["stock_movements"] if m["movement_type"] == "transfer_in"],
    lambda m: (m["reference_id"], m["warehouse_id"], m["product_id"]),
    lambda m: int(m["quantity_change"]),
)
expected_out = Counter()
expected_in = Counter()
for transfer in TABLES["stock_transfers"]:
    for item in transfer_items_by_id[transfer["transfer_id"]]:
        if int(item["quantity_shipped"]):
            expected_out[(transfer["transfer_id"], transfer["source_warehouse_id"], item["product_id"])] += int(item["quantity_shipped"])
        if int(item["quantity_received"]):
            expected_in[(transfer["transfer_id"], transfer["destination_warehouse_id"], item["product_id"])] += int(item["quantity_received"])
check("transfers:ledger_matches_shipped_and_received", [key for key in set(transfer_out) | set(expected_out) if transfer_out[key] != expected_out[key]] + [key for key in set(transfer_in) | set(expected_in) if transfer_in[key] != expected_in[key]])

return_items_by_id = defaultdict(list)
for item in TABLES["return_items"]:
    return_items_by_id[item["return_id"]].append(item)
return_errors = [ret["return_id"] for ret in TABLES["returns"]
                 if ret["customer_id"] != orders[ret["sales_order_id"]]["customer_id"]]
return_errors += [item["return_item_id"] for item in TABLES["return_items"]
                  if item["product_id"] != order_lines[item["sales_order_item_id"]]["product_id"]]
check("returns:customer_order_and_product_links", return_errors)
restock_expected = Counter()
for ret in TABLES["returns"]:
    for item in return_items_by_id[ret["return_id"]]:
        if item["disposition"] == "restock":
            restock_expected[(ret["return_id"], ret["warehouse_id"], item["product_id"])] += int(item["quantity"])
restock_movements = aggregate(
    [m for m in TABLES["stock_movements"] if m["movement_type"] == "customer_return"],
    lambda m: (m["reference_id"], m["warehouse_id"], m["product_id"]),
    lambda m: int(m["quantity_change"]),
)
check("returns:restock_movements_match_disposition",
      [key for key in set(restock_expected) | set(restock_movements)
       if restock_expected[key] != restock_movements[key]])

report_lines = ["SYNTHETIC ERP CSV VALIDATION", f"Tables: {len(TABLES)}", ""]
failed = 0
for name, n, detail in CHECKS:
    report_lines.append(f"{'PASS' if n == 0 else 'FAIL'}  {name}  ({detail or 'no violations'})")
    failed += n > 0
report_lines += ["", f"Checks: {len(CHECKS)} | failed checks: {failed}",
                 f"DATASET VALID: {'YES' if failed == 0 else 'NO'}"]
text = "\n".join(report_lines) + "\n"
print(text)
(OUT / "validation_report.txt").write_text(text, encoding="utf-8")
sys.exit(0 if failed == 0 else 1)

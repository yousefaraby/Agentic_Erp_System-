# Data Dictionary

Schema: `erp` (synthetic data only). All monetary columns are `NUMERIC(14,2)`,
all timestamps `TIMESTAMPTZ`. `created_at`/`updated_at` are present on the
entities that are edited over time.

---

## Organizational data

### `branches`
Physical sales locations. One row is the `headquarters`; the rest are `branch`.
- **PK:** `branch_id`; **unique:** `branch_code`.
- Columns: `name`, `branch_type` (`headquarters|branch|kiosk|outlet`), address
  fields, `opened_date`, `is_active`.
- **Referenced by:** `warehouses`, `employees`, `sales_orders`.

### `warehouses`
Stock locations. At least one `central`; others `regional`/`branch`/`returns`.
- **PK:** `warehouse_id`; **unique:** `warehouse_code`.
- `branch_id` is the legal/administrative owner where applicable; operational
  branch coverage is recorded in `branch_warehouse_services`.
- `capacity_units`.
- **Referenced by:** `inventory`, `stock_movements`, `stock_transfers`,
  `purchase_orders`, `shipments`, `returns`, sales order items.

### `branch_warehouse_services`
Effective-dated many-to-many branch/warehouse service map. `is_primary` marks
the default fulfilment location; `can_fulfill` and `can_transfer` describe
operational capabilities. A branch has one active primary warehouse; the
central DC may serve multiple branches.

### `departments`
Normalized department catalog referenced by `employees.department_id`.

### `inventory_reservations`, `inventory_reservation_items`
Active reservations for open sales orders. Active reservation quantities roll up
to `inventory.quantity_reserved`; reservation lines tie reserved stock pairs to
sales order lines. Only fully available orders remain in `reserved` status.

### `employees`
Staff across branches/warehouses.
- **PK:** `employee_id`; **unique:** `employee_code`, `email`.
- `branch_id` → `branches`; `warehouse_id` → `warehouses` (warehouse staff);
  `department_id` → `departments`;
  `manager_id` → `employees` (self-reference).

### `roles`, `permissions`, `role_permissions`
Role-based access control. `role_permissions` maps roles to permissions
(composite PK). Includes an `ai_agent_service` role for automated agents.

### `users`
Login accounts.
- **PK:** `user_id`; **unique:** `username`, `email`.
- `employee_id` → `employees`; `role_id` → `roles`.
- `password_hash` is a synthetic placeholder, never a real credential.

---

## Product & supplier data

### `product_categories`
Two-level hierarchy (root → leaf) via `parent_category_id` (self-reference).
- **PK:** `category_id`; **unique:** `category_code`.

### `products`
- **PK:** `product_id`; **unique:** `sku`.
- `category_id` → `product_categories`.
- `cost_price`, `list_price`, `reorder_point`, `reorder_quantity`,
  `safety_stock`, `weight_kg`.
- **Referenced by:** sales order items, inventory, movements, PO items, etc.

### `suppliers`
- **PK:** `supplier_id`; **unique:** `supplier_code`, `email`.
- `payment_terms` (`NET15..NET60|PREPAID|COD`), `default_lead_time_days`,
  `rating` (0–5).

### `supplier_products`
Supplier-specific pricing and lead time per product.
- **PK:** `supplier_product_id`; **unique:** `(supplier_id, product_id)`.
- `unit_cost`, `lead_time_days`, `min_order_qty`, `is_preferred`,
  `valid_from`/`valid_to`.
- Products with exactly one row here are "single-sourced" (supply risk).

---

## Customer data

### `customers`
- **PK:** `customer_id`; **unique:** `customer_code`, `email`.
- `customer_type` (`individual|business`) with a CHECK enforcing that
  individuals have names and businesses have a company name.
- `loyalty_tier`, `credit_limit`, `registration_date`.

---

## Sales data

### `sales_orders`
- **PK:** `sales_order_id`; **unique:** `order_number`.
- `branch_id` → `branches` (**every order has a branch**), `customer_id` →
  `customers`, `employee_id` → `employees`.
- `order_date`, `status`
  (`draft|pending|reserved|dispatched|delivered|cancelled`), `channel`
  (`in_store|online|phone|wholesale`).
- `subtotal`, `discount_amount`, `tax_amount`, `shipping_amount`,
  `total_amount`, `currency`, `cancelled_at`.
- CHECKs: a cancelled order must have `cancelled_at`, and only cancelled orders
  may have it.

### `sales_order_items`
- **PK:** `sales_order_item_id`; **unique:** `(sales_order_id, line_number)`.
- `product_id` → `products`, `warehouse_id` → `warehouses` (fulfilment source).
- `quantity`, `quantity_fulfilled` (≤ quantity), `unit_price`, `discount_rate`,
  `tax_rate`, `line_subtotal`, `line_total`.

### `payments`
- **PK:** `payment_id`; **unique:** `payment_number`.
- `sales_order_id`, `payment_date`, `amount`, `method`
  (`cash|card|bank_transfer|mobile_money|credit`), `status`
  (`pending|completed|failed|refunded`), `reference`.

### `shipments`
- **PK:** `shipment_id`; **unique:** `shipment_number`.
- `sales_order_id`, `warehouse_id`, `carrier`, `tracking_number`,
  `shipped_date`, `promised_date`, `delivered_date`, `status`
  (`pending|picking|dispatched|in_transit|delivered|failed|returned`),
  `shipping_cost`. CHECK: `delivered_date >= shipped_date`.

### `returns`
- **PK:** `return_id`; **unique:** `return_number`.
- `sales_order_id`, `customer_id`, `warehouse_id`, `return_date`, `reason`
  (7 values), `status` (`requested|approved|received|rejected|refunded`),
  `refund_amount`.

### `return_items`
- **PK:** `return_item_id`.
- `return_id` → `returns`, `sales_order_item_id` → `sales_order_items`,
  `product_id`.
- `quantity`, `unit_price`, `line_refund`, `item_condition`
  (`new|opened|damaged|used`), `disposition`
  (`restock|scrap|repair|return_to_supplier`). Only `restock` adds stock back.

### `payment_refunds`
Refund transactions linked to both a payment and the return they settle. The
generated data records item refunds including tax; each refund reconciles to
its return header and return lines.

---

## Inventory data

### `inventory`
Current stock position. **Unique per `(warehouse_id, product_id)`.**
- **PK:** `inventory_id`.
- `quantity_on_hand` (≥0), `quantity_reserved` (≤ on_hand), `reorder_point`,
  `last_counted_at`, `updated_at`.
- Invariant: `quantity_on_hand` equals the sum of `quantity_change` in
  `stock_movements` for the same pair.

### `stock_movements`
Append-only signed ledger; the source of truth for balances.
- **PK:** `movement_id` (BIGINT).
- `warehouse_id`, `product_id`, `movement_type`
  (`initial|sale|purchase_receipt|transfer_out|transfer_in|customer_return|adjustment|scrap`),
  `quantity_change` (signed, ≠0), `balance_after` (≥0).
- `reference_type` (`sales_order|purchase_order|goods_receipt|stock_transfer|return|manual`)
  and `reference_id` make every movement **traceable** (except `initial`).
- `unit_cost`, `movement_date`, `notes`.

### `stock_transfers`
Inter-warehouse transfer requests.
- **PK:** `transfer_id`; **unique:** `transfer_number`.
- `source_warehouse_id`, `destination_warehouse_id` (CHECK: different),
  `requested_by`, `approved_by`, `request_date`, `status`
  (`pending|approved|in_transit|completed|rejected|cancelled`),
  `dispatched_date`, `received_date`, `expected_date`, `total_quantity`,
  `reason` (`replenishment|rebalance|stockout_relief|seasonal|consolidation`).
- CHECK: completed transfers must have `received_date`.

### `stock_transfer_items`
- **PK:** `transfer_item_id`.
- `transfer_id`, `product_id`, `quantity_requested`, `quantity_shipped`
  (≤ requested), `quantity_received` (≤ shipped), `unit_cost`.
- `shipped > received` models an in-transit loss.

---

## Procurement data

### `purchase_orders`
- **PK:** `purchase_order_id`; **unique:** `po_number`.
- `supplier_id` → `suppliers`, `warehouse_id` → `warehouses` (destination),
  `requested_by`, `approved_by`, `status`
  (`draft|pending_approval|approved|ordered|partially_received|received|cancelled`),
  `order_date`, `expected_date`, totals, `currency`.
- CHECK: `expected_date >= order_date`.

### `purchase_order_items`
- **PK:** `po_item_id`; **unique:** `(purchase_order_id, line_number)`.
- `product_id`, `quantity_ordered`, `quantity_received` (≤ ordered),
  `unit_cost`, `line_total`.
- Invariant: `quantity_received` equals the sum of matching
  `goods_receipt_items.quantity_received`.

### `goods_receipts`
Goods actually delivered for a PO.
- **PK:** `receipt_id`; **unique:** `receipt_number`.
- `purchase_order_id`, `warehouse_id`, `received_by`, `received_date`,
  `status` (`partial|complete`).

### `goods_receipt_items`
- **PK:** `receipt_item_id`.
- `receipt_id`, `po_item_id`, `product_id`, `quantity_received`, `unit_cost`,
  `line_total` (CHECK: `line_total = quantity_received * unit_cost`).

---

## View: `v_inventory_position`
Joins `inventory`, `warehouses`, `products`, `product_categories` and exposes
`quantity_available` (`on_hand − reserved`) and `stock_status`
(`ok` / `reorder` / `out_of_stock`). Handy for the Inventory Agent.

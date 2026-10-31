-- =============================================================================
-- Inventory Agent queries (synthetic ERP dataset)
-- =============================================================================

-- 1. Which products are out of stock (anywhere), and where?
SELECT w.warehouse_code,
       w.name                                   AS warehouse,
       p.sku,
       p.name                                   AS product,
       i.quantity_on_hand,
       p.reorder_point
FROM erp.inventory i
JOIN erp.warehouses w ON w.warehouse_id = i.warehouse_id
JOIN erp.products   p ON p.product_id   = i.product_id
WHERE i.quantity_on_hand = 0
ORDER BY w.warehouse_code, p.sku;

-- 2. Which warehouses have surplus inventory?
SELECT w.warehouse_code,
       w.name                                   AS warehouse,
       COUNT(*)                                 AS surplus_pairs,
       SUM(i.quantity_on_hand)                  AS units_on_hand
FROM erp.inventory i
JOIN erp.warehouses w ON w.warehouse_id = i.warehouse_id
WHERE i.quantity_on_hand > i.reorder_point * 3
GROUP BY w.warehouse_code, w.name
ORDER BY units_on_hand DESC;

-- 3. Which branches/warehouses face potential shortages?
--    (current stock at or below the reorder point)
SELECT w.warehouse_id,
       w.warehouse_code,
       w.warehouse_type,
       p.product_id,
       p.sku,
       p.name                                   AS product,
       i.quantity_on_hand,
       i.reorder_point,
       (i.reorder_point - i.quantity_on_hand)   AS units_below_reorder
FROM erp.inventory i
JOIN erp.warehouses w ON w.warehouse_id = i.warehouse_id
JOIN erp.products   p ON p.product_id   = i.product_id
WHERE i.quantity_on_hand <= i.reorder_point
ORDER BY units_below_reorder DESC, w.warehouse_code
LIMIT 100;

-- 4. Which products should be transferred between warehouses?
--    Surplus at one warehouse, shortage/zero at another, for the same product.
SELECT p.sku,
       p.name                                   AS product,
       src.warehouse_code                       AS from_warehouse,
       src_qty.quantity_on_hand                 AS from_on_hand,
       dst.warehouse_code                       AS to_warehouse,
       dst_qty.quantity_on_hand                 AS to_on_hand,
       LEAST(src_qty.quantity_on_hand - src_qty.reorder_point * 2,
             dst_qty.reorder_point - dst_qty.quantity_on_hand) AS suggested_qty
FROM erp.inventory src_qty
JOIN erp.inventory dst_qty
     ON dst_qty.product_id = src_qty.product_id
    AND dst_qty.warehouse_id <> src_qty.warehouse_id
JOIN erp.warehouses src ON src.warehouse_id = src_qty.warehouse_id
JOIN erp.warehouses dst ON dst.warehouse_id = dst_qty.warehouse_id
JOIN erp.products   p   ON p.product_id = src_qty.product_id
WHERE src_qty.quantity_on_hand > src_qty.reorder_point * 3
  AND dst_qty.quantity_on_hand <= dst_qty.reorder_point
  AND LEAST(src_qty.quantity_on_hand - src_qty.reorder_point * 2,
            dst_qty.reorder_point - dst_qty.quantity_on_hand) > 0
ORDER BY suggested_qty DESC
LIMIT 50;

-- 5. What is the current stock position across all warehouses?
SELECT w.warehouse_code,
       w.warehouse_type,
       COUNT(*)                                         AS sku_count,
       SUM(i.quantity_on_hand)                          AS total_units,
       SUM(i.quantity_on_hand * p.cost_price)           AS inventory_value
FROM erp.inventory i
JOIN erp.warehouses w ON w.warehouse_id = i.warehouse_id
JOIN erp.products   p ON p.product_id   = i.product_id
GROUP BY w.warehouse_code, w.warehouse_type
ORDER BY total_units DESC;

-- 6. Low-turnover products (stock on hand but few sales in the period).
SELECT p.sku,
       p.name                                   AS product,
       SUM(i.quantity_on_hand)                  AS stock_on_hand,
       COALESCE(sold.units, 0)                  AS units_sold_12m
FROM erp.products p
JOIN erp.inventory i ON i.product_id = p.product_id
LEFT JOIN (
    SELECT soi.product_id, SUM(soi.quantity_fulfilled) AS units
    FROM erp.sales_order_items soi
    JOIN erp.sales_orders so ON so.sales_order_id = soi.sales_order_id
    WHERE so.status IN ('dispatched','delivered')
    GROUP BY soi.product_id
) sold ON sold.product_id = p.product_id
GROUP BY p.sku, p.name, sold.units
HAVING SUM(i.quantity_on_hand) > 50 AND COALESCE(sold.units, 0) < 20
ORDER BY stock_on_hand DESC;

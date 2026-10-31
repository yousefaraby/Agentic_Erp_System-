-- =============================================================================
-- Procurement Agent queries (synthetic ERP dataset)
-- =============================================================================

-- 1. Which products need replenishment?
--    Aggregate on-hand across warehouses versus reorder points.
SELECT p.product_id,
       p.sku,
       p.name                                     AS product,
       SUM(i.quantity_on_hand)                    AS total_on_hand,
       SUM(i.reorder_point)                       AS total_reorder_point,
       p.reorder_quantity
FROM erp.products p
JOIN erp.inventory i ON i.product_id = p.product_id
GROUP BY p.product_id, p.sku, p.name, p.reorder_quantity
HAVING SUM(i.quantity_on_hand) <= SUM(i.reorder_point)
ORDER BY (SUM(i.reorder_point) - SUM(i.quantity_on_hand)) DESC;

-- 2. Which suppliers offer the best combination of price and lead time?
--    Ranked by unit cost per product; cheaper and faster is better.
SELECT sp.product_id,
       p.sku,
       p.name                                     AS product,
       s.supplier_code,
       s.name                                     AS supplier,
       sp.unit_cost,
       sp.lead_time_days,
       sp.min_order_qty,
       sp.is_preferred
FROM erp.supplier_products sp
JOIN erp.suppliers s ON s.supplier_id = sp.supplier_id
JOIN erp.products  p ON p.product_id  = sp.product_id
ORDER BY sp.product_id, sp.unit_cost ASC, sp.lead_time_days ASC;

-- 2b. Best single source per product (lowest cost, then fastest lead time).
SELECT DISTINCT ON (sp.product_id)
       sp.product_id,
       p.sku,
       s.name                                     AS best_supplier,
       sp.unit_cost,
       sp.lead_time_days
FROM erp.supplier_products sp
JOIN erp.suppliers s ON s.supplier_id = sp.supplier_id
JOIN erp.products  p ON p.product_id  = sp.product_id
ORDER BY sp.product_id, sp.unit_cost ASC, sp.lead_time_days ASC;

-- 3. Which suppliers have the highest delay rates?
--    A delivery is late when the goods receipt is after the PO expected date.
SELECT s.supplier_id,
       s.supplier_code,
       s.name                                                       AS supplier,
       COUNT(gr.receipt_id)                                         AS deliveries,
       SUM(CASE WHEN gr.received_date::date > po.expected_date
                THEN 1 ELSE 0 END)                                  AS late_deliveries,
       ROUND(SUM(CASE WHEN gr.received_date::date > po.expected_date
                      THEN 1 ELSE 0 END)::numeric
             / NULLIF(COUNT(gr.receipt_id), 0), 4)                  AS delay_rate,
       ROUND(AVG(gr.received_date::date - po.expected_date), 2)     AS avg_days_late
FROM erp.suppliers s
JOIN erp.purchase_orders po ON po.supplier_id = s.supplier_id
JOIN erp.goods_receipts  gr ON gr.purchase_order_id = po.purchase_order_id
GROUP BY s.supplier_id, s.supplier_code, s.name
HAVING COUNT(gr.receipt_id) > 0
ORDER BY delay_rate DESC NULLS LAST;

-- 4. What purchase orders require attention?
--    Awaiting approval, partially received, or overdue.
SELECT po.po_number,
       s.name                                     AS supplier,
       w.warehouse_code,
       po.status,
       po.order_date::date,
       po.expected_date,
       po.total_amount,
       CASE
           WHEN po.status = 'pending_approval' THEN 'awaiting approval'
           WHEN po.status = 'partially_received' THEN 'partially received'
           WHEN po.expected_date < CURRENT_DATE
                AND po.status NOT IN ('received','cancelled') THEN 'overdue'
           ELSE 'review'
       END                                        AS attention
FROM erp.purchase_orders po
JOIN erp.suppliers  s ON s.supplier_id  = po.supplier_id
JOIN erp.warehouses w ON w.warehouse_id = po.warehouse_id
WHERE po.status IN ('pending_approval', 'partially_received', 'ordered', 'approved')
  AND (po.status IN ('pending_approval', 'partially_received')
       OR po.expected_date < po.order_date::date + INTERVAL '60 days')
ORDER BY po.expected_date;

-- 5. Products dependent on a single supplier (supply risk).
SELECT p.sku,
       p.name                                     AS product,
       COUNT(sp.supplier_id)                      AS supplier_count,
       MIN(s.name)                                AS only_supplier
FROM erp.products p
JOIN erp.supplier_products sp ON sp.product_id = p.product_id
JOIN erp.suppliers s ON s.supplier_id = sp.supplier_id
GROUP BY p.sku, p.name
HAVING COUNT(sp.supplier_id) = 1
ORDER BY p.sku;

-- =============================================================================
-- Sales Agent queries (synthetic ERP dataset)
-- Revenue is recognised on non-cancelled orders that were at least dispatched,
-- using the fulfilled quantity where partial fulfilment occurred.
-- =============================================================================

-- 1. Which products generated the highest revenue?
SELECT p.product_id,
       p.sku,
       p.name,
       c.name                                   AS category,
       SUM(soi.quantity_fulfilled * soi.unit_price
           * (1 - soi.discount_rate))           AS revenue,
       SUM(soi.quantity_fulfilled)              AS units_sold
FROM erp.sales_order_items soi
JOIN erp.sales_orders        so ON so.sales_order_id = soi.sales_order_id
JOIN erp.products            p  ON p.product_id = soi.product_id
JOIN erp.product_categories  c  ON c.category_id = p.category_id
WHERE so.status IN ('dispatched', 'delivered')
GROUP BY p.product_id, p.sku, p.name, c.name
ORDER BY revenue DESC
LIMIT 25;

-- 2. Which branch performed best?
SELECT b.branch_id,
       b.branch_code,
       b.name,
       COUNT(DISTINCT so.sales_order_id)                                   AS orders,
       SUM(soi.quantity_fulfilled * soi.unit_price * (1 - soi.discount_rate)) AS revenue,
       ROUND(AVG(so.total_amount), 2)                                      AS avg_order_value
FROM erp.sales_orders so
JOIN erp.branches b ON b.branch_id = so.branch_id
JOIN erp.sales_order_items soi ON soi.sales_order_id = so.sales_order_id
WHERE so.status IN ('dispatched', 'delivered')
GROUP BY b.branch_id, b.branch_code, b.name
ORDER BY revenue DESC;

-- 3. Daily and weekly sales patterns.
SELECT so.order_date::date                            AS sales_day,
       TO_CHAR(so.order_date, 'Dy')                   AS weekday,
       EXTRACT(ISODOW FROM so.order_date)             AS iso_dow,
       COUNT(DISTINCT so.sales_order_id)              AS orders,
       SUM(so.total_amount)                           AS gross_sales
FROM erp.sales_orders so
WHERE so.status <> 'cancelled'
GROUP BY 1, 2, 3
ORDER BY 1;

-- 3b. Average sales by day of week (weekday vs weekend demand).
SELECT TO_CHAR(so.order_date, 'Dy')      AS weekday,
       EXTRACT(ISODOW FROM so.order_date) AS iso_dow,
       COUNT(*)                           AS orders,
       ROUND(AVG(so.total_amount), 2)     AS avg_order_value
FROM erp.sales_orders so
WHERE so.status <> 'cancelled'
GROUP BY 1, 2
ORDER BY 2;

-- 4. Which products have the highest return rates?
SELECT p.product_id,
       p.sku,
       p.name,
       COALESCE(sold.units, 0)                                        AS units_sold,
       COALESCE(ret.units_returned, 0)                                AS units_returned,
       CASE WHEN COALESCE(sold.units, 0) = 0 THEN NULL
            ELSE ROUND(ret.units_returned::numeric / sold.units, 4)
       END                                                            AS return_rate
FROM erp.products p
LEFT JOIN (
    SELECT soi.product_id, SUM(soi.quantity) AS units
    FROM erp.sales_order_items soi
    JOIN erp.sales_orders so ON so.sales_order_id = soi.sales_order_id
    WHERE so.status <> 'cancelled'
    GROUP BY soi.product_id
) sold ON sold.product_id = p.product_id
LEFT JOIN (
    SELECT ri.product_id, SUM(ri.quantity) AS units_returned
    FROM erp.return_items ri
    GROUP BY ri.product_id
) ret ON ret.product_id = p.product_id
WHERE COALESCE(sold.units, 0) > 0
ORDER BY return_rate DESC NULLS LAST
LIMIT 25;

-- 5. How has sales performance changed over time (monthly trend)?
SELECT DATE_TRUNC('month', so.order_date)::date AS month,
       COUNT(DISTINCT so.sales_order_id)        AS orders,
       SUM(so.total_amount)                     AS gross_sales
FROM erp.sales_orders so
WHERE so.status <> 'cancelled'
GROUP BY 1
ORDER BY 1;

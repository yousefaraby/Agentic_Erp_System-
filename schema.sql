-- =============================================================================
-- Agentic ERP - PostgreSQL schema (synthetic dataset)
-- Fully synthetic data. No record represents a real company or transaction.
--
-- Conventions
--   * All monetary columns are NUMERIC(14,2) - exact decimal arithmetic.
--   * All quantities are INTEGER (whole retail units).
--   * Mutable entity tables carry created_at / updated_at TIMESTAMPTZ columns.
--   * Enumerated business states are enforced with CHECK constraints.
--   * Every foreign key references an existing record (declared inline).
--
-- Run:  psql -f schema.sql
-- The generator drops & recreates the schema, so this file is idempotent as a
-- unit (CREATE SCHEMA ... CASCADE on the erp schema drops everything in it).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Reset: drop and recreate the dedicated application schema (idempotent).
-- ---------------------------------------------------------------------------
DROP SCHEMA IF EXISTS erp CASCADE;
CREATE SCHEMA erp;
SET search_path TO erp, public;

-- ===========================================================================
-- ORGANIZATIONAL DATA
-- ===========================================================================

CREATE TABLE branches (
    branch_id       INTEGER      PRIMARY KEY,
    branch_code     VARCHAR(16)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    branch_type     VARCHAR(20)  NOT NULL
                    CHECK (branch_type IN ('headquarters', 'branch', 'kiosk', 'outlet')),
    address_line    VARCHAR(200) NOT NULL,
    city            VARCHAR(80)  NOT NULL,
    region          VARCHAR(80)  NOT NULL,
    country         VARCHAR(80)  NOT NULL,
    phone           VARCHAR(32),
    email           VARCHAR(120),
    opened_date     DATE         NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_branches_city ON branches (city);

CREATE TABLE warehouses (
    warehouse_id    INTEGER      PRIMARY KEY,
    warehouse_code  VARCHAR(16)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    warehouse_type  VARCHAR(20)  NOT NULL
                    CHECK (warehouse_type IN ('central', 'regional', 'branch', 'returns')),
    branch_id       INTEGER      REFERENCES branches (branch_id) ON DELETE SET NULL,
    address_line    VARCHAR(200) NOT NULL,
    city            VARCHAR(80)  NOT NULL,
    region          VARCHAR(80)  NOT NULL,
    country         VARCHAR(80)  NOT NULL,
    capacity_units  INTEGER      NOT NULL CHECK (capacity_units > 0),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_warehouses_branch ON warehouses (branch_id);
CREATE INDEX idx_warehouses_type   ON warehouses (warehouse_type);

-- Explicitly records which warehouses serve each branch. A warehouse can
-- serve several branches and a branch can use several warehouses.
CREATE TABLE branch_warehouse_services (
    branch_id       INTEGER      NOT NULL REFERENCES branches (branch_id) ON DELETE CASCADE,
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id) ON DELETE CASCADE,
    is_primary      BOOLEAN      NOT NULL DEFAULT FALSE,
    can_fulfill     BOOLEAN      NOT NULL DEFAULT TRUE,
    can_transfer    BOOLEAN      NOT NULL DEFAULT TRUE,
    valid_from      DATE         NOT NULL,
    valid_to        DATE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, warehouse_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE UNIQUE INDEX uq_branch_primary_warehouse
    ON branch_warehouse_services (branch_id) WHERE is_primary AND valid_to IS NULL;
CREATE INDEX idx_branch_warehouse_service_wh
    ON branch_warehouse_services (warehouse_id, can_fulfill);

CREATE TABLE departments (
    department_id   INTEGER      PRIMARY KEY,
    department_code VARCHAR(32)  NOT NULL UNIQUE,
    name            VARCHAR(80)  NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE employees (
    employee_id     INTEGER      PRIMARY KEY,
    employee_code   VARCHAR(20)  NOT NULL UNIQUE,
    branch_id       INTEGER      NOT NULL REFERENCES branches (branch_id),
    warehouse_id    INTEGER      REFERENCES warehouses (warehouse_id),
    first_name      VARCHAR(60)  NOT NULL,
    last_name       VARCHAR(60)  NOT NULL,
    email           VARCHAR(120) NOT NULL UNIQUE,
    phone           VARCHAR(32),
    job_title       VARCHAR(80)  NOT NULL,
    department_id   INTEGER      NOT NULL REFERENCES departments (department_id),
    hire_date       DATE         NOT NULL,
    termination_date DATE,
    manager_id      INTEGER      REFERENCES employees (employee_id),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (termination_date IS NULL OR termination_date >= hire_date)
);
CREATE INDEX idx_employees_branch ON employees (branch_id);
CREATE INDEX idx_employees_wh     ON employees (warehouse_id);

CREATE TABLE roles (
    role_id         INTEGER      PRIMARY KEY,
    role_name       VARCHAR(40)  NOT NULL UNIQUE,
    description     VARCHAR(200) NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE permissions (
    permission_id   INTEGER      PRIMARY KEY,
    permission_key  VARCHAR(60)  NOT NULL UNIQUE,
    description     VARCHAR(200) NOT NULL
);

CREATE TABLE role_permissions (
    role_id         INTEGER      NOT NULL REFERENCES roles (role_id) ON DELETE CASCADE,
    permission_id   INTEGER      NOT NULL REFERENCES permissions (permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE users (
    user_id         INTEGER      PRIMARY KEY,
    username        VARCHAR(60)  NOT NULL UNIQUE,
    email           VARCHAR(120) NOT NULL UNIQUE,
    password_hash   VARCHAR(128) NOT NULL,
    employee_id     INTEGER      NOT NULL REFERENCES employees (employee_id),
    role_id         INTEGER      NOT NULL REFERENCES roles (role_id),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_employee ON users (employee_id);
CREATE INDEX idx_users_role     ON users (role_id);

-- ===========================================================================
-- PRODUCT & SUPPLIER DATA
-- ===========================================================================

CREATE TABLE product_categories (
    category_id         INTEGER      PRIMARY KEY,
    category_code       VARCHAR(24)  NOT NULL UNIQUE,
    name                VARCHAR(80)  NOT NULL,
    parent_category_id  INTEGER      REFERENCES product_categories (category_id),
    description         VARCHAR(200),
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (parent_category_id IS NULL OR parent_category_id <> category_id)
);
CREATE INDEX idx_categories_parent ON product_categories (parent_category_id);

CREATE TABLE products (
    product_id      INTEGER      PRIMARY KEY,
    sku             VARCHAR(24)  NOT NULL UNIQUE,
    name            VARCHAR(160) NOT NULL,
    category_id     INTEGER      NOT NULL REFERENCES product_categories (category_id),
    description     VARCHAR(300),
    unit_of_measure VARCHAR(12)  NOT NULL DEFAULT 'EA',
    cost_price      NUMERIC(14,2) NOT NULL CHECK (cost_price >= 0),
    list_price      NUMERIC(14,2) NOT NULL CHECK (list_price >= 0),
    reorder_point   INTEGER      NOT NULL CHECK (reorder_point >= 0),
    reorder_quantity INTEGER     NOT NULL CHECK (reorder_quantity > 0),
    safety_stock    INTEGER      NOT NULL CHECK (safety_stock >= 0),
    weight_kg       NUMERIC(8,3) CHECK (weight_kg IS NULL OR weight_kg >= 0),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_products_category ON products (category_id);
CREATE INDEX idx_products_active   ON products (is_active);

CREATE TABLE suppliers (
    supplier_id     INTEGER      PRIMARY KEY,
    supplier_code   VARCHAR(20)  NOT NULL UNIQUE,
    name            VARCHAR(140) NOT NULL,
    contact_name    VARCHAR(100) NOT NULL,
    email           VARCHAR(120) NOT NULL UNIQUE,
    phone           VARCHAR(32)  NOT NULL,
    address_line    VARCHAR(200) NOT NULL,
    city            VARCHAR(80)  NOT NULL,
    country         VARCHAR(80)  NOT NULL,
    payment_terms   VARCHAR(20)  NOT NULL
                    CHECK (payment_terms IN ('NET15','NET30','NET45','NET60','PREPAID','COD')),
    default_lead_time_days INTEGER NOT NULL CHECK (default_lead_time_days >= 0),
    rating          NUMERIC(3,2) NOT NULL CHECK (rating >= 0 AND rating <= 5),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_suppliers_country ON suppliers (country);

CREATE TABLE supplier_products (
    supplier_product_id INTEGER      PRIMARY KEY,
    supplier_id     INTEGER      NOT NULL REFERENCES suppliers (supplier_id),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    supplier_sku    VARCHAR(40)  NOT NULL,
    unit_cost       NUMERIC(14,2) NOT NULL CHECK (unit_cost >= 0),
    lead_time_days  INTEGER      NOT NULL CHECK (lead_time_days >= 0),
    min_order_qty   INTEGER      NOT NULL CHECK (min_order_qty > 0),
    is_preferred    BOOLEAN      NOT NULL DEFAULT FALSE,
    valid_from      DATE         NOT NULL,
    valid_to        DATE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (supplier_id, product_id),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX idx_supplier_products_product  ON supplier_products (product_id);
CREATE INDEX idx_supplier_products_supplier ON supplier_products (supplier_id);

-- ===========================================================================
-- CUSTOMER DATA
-- ===========================================================================

CREATE TABLE customers (
    customer_id     INTEGER      PRIMARY KEY,
    customer_code   VARCHAR(20)  NOT NULL UNIQUE,
    customer_type   VARCHAR(16)  NOT NULL CHECK (customer_type IN ('individual','business')),
    first_name      VARCHAR(60),
    last_name       VARCHAR(60),
    company_name    VARCHAR(160),
    email           VARCHAR(120) NOT NULL UNIQUE,
    phone           VARCHAR(32),
    address_line    VARCHAR(200) NOT NULL,
    city            VARCHAR(80)  NOT NULL,
    region          VARCHAR(80)  NOT NULL,
    country         VARCHAR(80)  NOT NULL,
    loyalty_tier    VARCHAR(16)  NOT NULL DEFAULT 'none'
                    CHECK (loyalty_tier IN ('none','bronze','silver','gold','platinum')),
    credit_limit    NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (credit_limit >= 0),
    registration_date DATE      NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (
        (customer_type = 'individual' AND first_name IS NOT NULL AND last_name IS NOT NULL)
        OR
        (customer_type = 'business'   AND company_name IS NOT NULL)
    )
);
CREATE INDEX idx_customers_city ON customers (city);
CREATE INDEX idx_customers_type ON customers (customer_type);

-- ===========================================================================
-- SALES DATA
-- ===========================================================================

CREATE TABLE sales_orders (
    sales_order_id  INTEGER      PRIMARY KEY,
    order_number    VARCHAR(24)  NOT NULL UNIQUE,
    branch_id       INTEGER      NOT NULL REFERENCES branches (branch_id),
    customer_id     INTEGER      NOT NULL REFERENCES customers (customer_id),
    employee_id     INTEGER      REFERENCES employees (employee_id),
    order_date      TIMESTAMPTZ  NOT NULL,
    status          VARCHAR(16)  NOT NULL
                    CHECK (status IN ('draft','pending','reserved','dispatched','delivered','cancelled')),
    channel         VARCHAR(16)  NOT NULL CHECK (channel IN ('in_store','online','phone','wholesale')),
    subtotal        NUMERIC(14,2) NOT NULL CHECK (subtotal >= 0),
    discount_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    tax_amount      NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    shipping_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (shipping_amount >= 0),
    total_amount    NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),
    currency        VARCHAR(3)   NOT NULL DEFAULT 'USD',
    notes           VARCHAR(300),
    cancelled_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL),
    CHECK (cancelled_at IS NULL OR status = 'cancelled')
);
CREATE INDEX idx_so_branch   ON sales_orders (branch_id);
CREATE INDEX idx_so_customer ON sales_orders (customer_id);
CREATE INDEX idx_so_date     ON sales_orders (order_date);
CREATE INDEX idx_so_status   ON sales_orders (status);

CREATE TABLE sales_order_items (
    sales_order_item_id INTEGER      PRIMARY KEY,
    sales_order_id  INTEGER      NOT NULL REFERENCES sales_orders (sales_order_id) ON DELETE CASCADE,
    line_number     INTEGER      NOT NULL CHECK (line_number > 0),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    quantity        INTEGER      NOT NULL CHECK (quantity > 0),
    quantity_fulfilled INTEGER   NOT NULL DEFAULT 0 CHECK (quantity_fulfilled >= 0),
    unit_price      NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
    discount_rate   NUMERIC(5,4) NOT NULL DEFAULT 0 CHECK (discount_rate >= 0 AND discount_rate <= 1),
    tax_rate        NUMERIC(5,4) NOT NULL DEFAULT 0 CHECK (tax_rate >= 0 AND tax_rate <= 1),
    line_subtotal   NUMERIC(14,2) NOT NULL CHECK (line_subtotal >= 0),
    line_total      NUMERIC(14,2) NOT NULL CHECK (line_total >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (sales_order_id, line_number),
    CHECK (quantity_fulfilled <= quantity)
);
CREATE INDEX idx_soi_order   ON sales_order_items (sales_order_id);
CREATE INDEX idx_soi_product ON sales_order_items (product_id);
CREATE INDEX idx_soi_wh      ON sales_order_items (warehouse_id);

CREATE TABLE payments (
    payment_id      INTEGER      PRIMARY KEY,
    payment_number  VARCHAR(24)  NOT NULL UNIQUE,
    sales_order_id  INTEGER      NOT NULL REFERENCES sales_orders (sales_order_id),
    payment_date    TIMESTAMPTZ  NOT NULL,
    amount          NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    method          VARCHAR(20)  NOT NULL
                    CHECK (method IN ('cash','card','bank_transfer','mobile_money','credit')),
    status          VARCHAR(16)  NOT NULL CHECK (status IN ('pending','completed','failed','refunded')),
    reference       VARCHAR(40),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_payments_order ON payments (sales_order_id);
CREATE INDEX idx_payments_date  ON payments (payment_date);

CREATE TABLE shipments (
    shipment_id     INTEGER      PRIMARY KEY,
    shipment_number VARCHAR(24)  NOT NULL UNIQUE,
    sales_order_id  INTEGER      NOT NULL REFERENCES sales_orders (sales_order_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    carrier         VARCHAR(60)  NOT NULL,
    tracking_number VARCHAR(40),
    shipped_date    TIMESTAMPTZ,
    promised_date   DATE         NOT NULL,
    delivered_date  TIMESTAMPTZ,
    status          VARCHAR(20)  NOT NULL
                    CHECK (status IN ('pending','picking','dispatched','in_transit','delivered','failed','returned')),
    shipping_cost   NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (shipping_cost >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (delivered_date IS NULL OR shipped_date IS NULL OR delivered_date >= shipped_date)
);
CREATE INDEX idx_shipments_order ON shipments (sales_order_id);
CREATE INDEX idx_shipments_status ON shipments (status);

CREATE TABLE returns (
    return_id       INTEGER      PRIMARY KEY,
    return_number   VARCHAR(24)  NOT NULL UNIQUE,
    sales_order_id  INTEGER      NOT NULL REFERENCES sales_orders (sales_order_id),
    customer_id     INTEGER      NOT NULL REFERENCES customers (customer_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    return_date     TIMESTAMPTZ  NOT NULL,
    reason          VARCHAR(30)  NOT NULL
                    CHECK (reason IN ('defective','wrong_item','not_as_described','changed_mind',
                                      'damaged_in_transit','late_delivery','other')),
    status          VARCHAR(16)  NOT NULL CHECK (status IN ('requested','approved','received','rejected','refunded')),
    refund_amount   NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (refund_amount >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_returns_order    ON returns (sales_order_id);
CREATE INDEX idx_returns_customer ON returns (customer_id);
CREATE INDEX idx_returns_date     ON returns (return_date);

CREATE TABLE return_items (
    return_item_id  INTEGER      PRIMARY KEY,
    return_id       INTEGER      NOT NULL REFERENCES returns (return_id) ON DELETE CASCADE,
    sales_order_item_id INTEGER  NOT NULL REFERENCES sales_order_items (sales_order_item_id),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    quantity        INTEGER      NOT NULL CHECK (quantity > 0),
    unit_price      NUMERIC(14,2) NOT NULL CHECK (unit_price >= 0),
    line_refund     NUMERIC(14,2) NOT NULL CHECK (line_refund >= 0),
    item_condition  VARCHAR(16)  NOT NULL CHECK (item_condition IN ('new','opened','damaged','used')),
    disposition     VARCHAR(20)  NOT NULL CHECK (disposition IN ('restock','scrap','repair','return_to_supplier')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_return_items_return  ON return_items (return_id);
CREATE INDEX idx_return_items_product ON return_items (product_id);

CREATE TABLE payment_refunds (
    refund_id       INTEGER      PRIMARY KEY,
    refund_number   VARCHAR(24)  NOT NULL UNIQUE,
    payment_id      INTEGER      NOT NULL REFERENCES payments (payment_id),
    return_id       INTEGER      NOT NULL REFERENCES returns (return_id),
    refund_date     TIMESTAMPTZ  NOT NULL,
    amount          NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    status          VARCHAR(16)  NOT NULL CHECK (status IN ('pending','completed','failed')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_payment_refunds_payment ON payment_refunds (payment_id);
CREATE INDEX idx_payment_refunds_return ON payment_refunds (return_id);

-- ===========================================================================
-- INVENTORY MANAGEMENT
-- ===========================================================================

-- Current on-hand position, one row per (warehouse, product).
CREATE TABLE inventory (
    inventory_id       INTEGER      PRIMARY KEY,
    warehouse_id       INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    product_id         INTEGER      NOT NULL REFERENCES products (product_id),
    quantity_on_hand   INTEGER      NOT NULL CHECK (quantity_on_hand >= 0),
    quantity_reserved  INTEGER      NOT NULL DEFAULT 0 CHECK (quantity_reserved >= 0),
    reorder_point      INTEGER      NOT NULL CHECK (reorder_point >= 0),
    last_counted_at    DATE         NOT NULL,
    updated_at         TIMESTAMPTZ  NOT NULL,
    UNIQUE (warehouse_id, product_id),
    CHECK (quantity_reserved <= quantity_on_hand)
);
CREATE INDEX idx_inventory_product ON inventory (product_id);
CREATE INDEX idx_inventory_wh      ON inventory (warehouse_id);

CREATE TABLE inventory_reservations (
    reservation_id  INTEGER      PRIMARY KEY,
    sales_order_id  INTEGER      NOT NULL UNIQUE REFERENCES sales_orders (sales_order_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    status          VARCHAR(16)  NOT NULL CHECK (status IN ('active','released','consumed','expired')),
    reserved_at     TIMESTAMPTZ  NOT NULL,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at >= reserved_at)
);
CREATE INDEX idx_inventory_reservations_active
    ON inventory_reservations (warehouse_id, sales_order_id) WHERE status = 'active';

CREATE TABLE inventory_reservation_items (
    reservation_item_id INTEGER      PRIMARY KEY,
    reservation_id      INTEGER      NOT NULL REFERENCES inventory_reservations (reservation_id) ON DELETE CASCADE,
    sales_order_item_id INTEGER      NOT NULL REFERENCES sales_order_items (sales_order_item_id),
    warehouse_id        INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    product_id          INTEGER      NOT NULL REFERENCES products (product_id),
    quantity            INTEGER      NOT NULL CHECK (quantity > 0),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (reservation_id, sales_order_item_id),
    FOREIGN KEY (warehouse_id, product_id) REFERENCES inventory (warehouse_id, product_id)
);
CREATE INDEX idx_reservation_items_stock ON inventory_reservation_items (warehouse_id, product_id);

-- Append-only ledger. quantity_change is signed. Every movement is traceable to
-- the business transaction that caused it via (reference_type, reference_id).
CREATE TABLE stock_movements (
    movement_id     BIGINT       PRIMARY KEY,
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    movement_type   VARCHAR(24)  NOT NULL
                    CHECK (movement_type IN ('initial','sale','purchase_receipt','transfer_out',
                                             'transfer_in','customer_return','adjustment','scrap')),
    quantity_change INTEGER      NOT NULL CHECK (quantity_change <> 0),
    balance_after   INTEGER      NOT NULL CHECK (balance_after >= 0),
    reference_type  VARCHAR(24)
                    CHECK (reference_type IN ('sales_order','purchase_order','goods_receipt',
                                              'stock_transfer','return','manual')),
    reference_id    BIGINT,
    unit_cost       NUMERIC(14,2) CHECK (unit_cost IS NULL OR unit_cost >= 0),
    movement_date   TIMESTAMPTZ  NOT NULL,
    notes           VARCHAR(200),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_sm_wh_product ON stock_movements (warehouse_id, product_id);
CREATE INDEX idx_sm_date       ON stock_movements (movement_date);
CREATE INDEX idx_sm_ref        ON stock_movements (reference_type, reference_id);
CREATE INDEX idx_sm_type       ON stock_movements (movement_type);

CREATE TABLE stock_transfers (
    transfer_id         INTEGER      PRIMARY KEY,
    transfer_number     VARCHAR(24)  NOT NULL UNIQUE,
    source_warehouse_id INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    destination_warehouse_id INTEGER  NOT NULL REFERENCES warehouses (warehouse_id),
    requested_by        INTEGER      NOT NULL REFERENCES employees (employee_id),
    approved_by         INTEGER      REFERENCES employees (employee_id),
    request_date        TIMESTAMPTZ  NOT NULL,
    status              VARCHAR(16)  NOT NULL
                        CHECK (status IN ('pending','approved','in_transit','completed','rejected','cancelled')),
    dispatched_date     TIMESTAMPTZ,
    received_date       TIMESTAMPTZ,
    expected_date       DATE         NOT NULL,
    total_quantity      INTEGER      NOT NULL CHECK (total_quantity > 0),
    reason              VARCHAR(30)  NOT NULL
                        CHECK (reason IN ('replenishment','rebalance','stockout_relief','seasonal','consolidation')),
    notes               VARCHAR(200),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (source_warehouse_id <> destination_warehouse_id),
    CHECK (status <> 'completed' OR received_date IS NOT NULL)
);
CREATE INDEX idx_transfers_source ON stock_transfers (source_warehouse_id);
CREATE INDEX idx_transfers_dest   ON stock_transfers (destination_warehouse_id);
CREATE INDEX idx_transfers_status ON stock_transfers (status);

CREATE TABLE stock_transfer_items (
    transfer_item_id   INTEGER      PRIMARY KEY,
    transfer_id        INTEGER      NOT NULL REFERENCES stock_transfers (transfer_id) ON DELETE CASCADE,
    product_id         INTEGER      NOT NULL REFERENCES products (product_id),
    quantity_requested INTEGER     NOT NULL CHECK (quantity_requested > 0),
    quantity_shipped   INTEGER      NOT NULL DEFAULT 0 CHECK (quantity_shipped >= 0),
    quantity_received  INTEGER      NOT NULL DEFAULT 0 CHECK (quantity_received >= 0),
    unit_cost          NUMERIC(14,2) NOT NULL CHECK (unit_cost >= 0),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (quantity_shipped <= quantity_requested),
    CHECK (quantity_received <= quantity_shipped)
);
CREATE INDEX idx_transfer_items_transfer ON stock_transfer_items (transfer_id);
CREATE INDEX idx_transfer_items_product  ON stock_transfer_items (product_id);

-- ===========================================================================
-- PROCUREMENT
-- ===========================================================================

CREATE TABLE purchase_orders (
    purchase_order_id INTEGER      PRIMARY KEY,
    po_number       VARCHAR(24)  NOT NULL UNIQUE,
    supplier_id     INTEGER      NOT NULL REFERENCES suppliers (supplier_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    requested_by    INTEGER      REFERENCES employees (employee_id),
    approved_by     INTEGER      REFERENCES employees (employee_id),
    status          VARCHAR(24)  NOT NULL
                    CHECK (status IN ('draft','pending_approval','approved','ordered',
                                      'partially_received','received','cancelled')),
    order_date      TIMESTAMPTZ  NOT NULL,
    expected_date   DATE         NOT NULL,
    subtotal        NUMERIC(14,2) NOT NULL CHECK (subtotal >= 0),
    tax_amount      NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    shipping_amount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (shipping_amount >= 0),
    total_amount    NUMERIC(14,2) NOT NULL CHECK (total_amount >= 0),
    currency        VARCHAR(3)   NOT NULL DEFAULT 'USD',
    notes           VARCHAR(300),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (expected_date >= order_date::date)
);
CREATE INDEX idx_po_supplier  ON purchase_orders (supplier_id);
CREATE INDEX idx_po_warehouse ON purchase_orders (warehouse_id);
CREATE INDEX idx_po_status    ON purchase_orders (status);
CREATE INDEX idx_po_date      ON purchase_orders (order_date);

CREATE TABLE purchase_order_items (
    po_item_id      INTEGER      PRIMARY KEY,
    purchase_order_id INTEGER    NOT NULL REFERENCES purchase_orders (purchase_order_id) ON DELETE CASCADE,
    line_number     INTEGER      NOT NULL CHECK (line_number > 0),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    quantity_ordered INTEGER     NOT NULL CHECK (quantity_ordered > 0),
    quantity_received INTEGER    NOT NULL DEFAULT 0 CHECK (quantity_received >= 0),
    unit_cost       NUMERIC(14,2) NOT NULL CHECK (unit_cost >= 0),
    line_total      NUMERIC(14,2) NOT NULL CHECK (line_total >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (purchase_order_id, line_number),
    CHECK (quantity_received <= quantity_ordered)
);
CREATE INDEX idx_poi_po      ON purchase_order_items (purchase_order_id);
CREATE INDEX idx_poi_product ON purchase_order_items (product_id);

CREATE TABLE goods_receipts (
    receipt_id      INTEGER      PRIMARY KEY,
    receipt_number  VARCHAR(24)  NOT NULL UNIQUE,
    purchase_order_id INTEGER    NOT NULL REFERENCES purchase_orders (purchase_order_id),
    warehouse_id    INTEGER      NOT NULL REFERENCES warehouses (warehouse_id),
    received_by     INTEGER      REFERENCES employees (employee_id),
    received_date   TIMESTAMPTZ  NOT NULL,
    status          VARCHAR(16)  NOT NULL CHECK (status IN ('partial','complete')),
    notes           VARCHAR(200),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_gr_po ON goods_receipts (purchase_order_id);

CREATE TABLE goods_receipt_items (
    receipt_item_id INTEGER      PRIMARY KEY,
    receipt_id      INTEGER      NOT NULL REFERENCES goods_receipts (receipt_id) ON DELETE CASCADE,
    po_item_id      INTEGER      NOT NULL REFERENCES purchase_order_items (po_item_id),
    product_id      INTEGER      NOT NULL REFERENCES products (product_id),
    quantity_received INTEGER    NOT NULL CHECK (quantity_received > 0),
    unit_cost       NUMERIC(14,2) NOT NULL CHECK (unit_cost >= 0),
    line_total      NUMERIC(14,2) NOT NULL CHECK (line_total >= 0),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CHECK (line_total = quantity_received * unit_cost)
);
CREATE INDEX idx_gri_receipt ON goods_receipt_items (receipt_id);
CREATE INDEX idx_gri_poitem  ON goods_receipt_items (po_item_id);

-- ===========================================================================
-- Convenience view: available-to-promise stock
-- ===========================================================================
CREATE OR REPLACE VIEW v_inventory_position AS
SELECT
    i.inventory_id,
    i.warehouse_id,
    w.warehouse_code,
    w.warehouse_type,
    w.branch_id,
    i.product_id,
    p.sku,
    p.name AS product_name,
    c.name AS category_name,
    i.quantity_on_hand,
    i.quantity_reserved,
    (i.quantity_on_hand - i.quantity_reserved) AS quantity_available,
    i.reorder_point,
    p.reorder_quantity,
    CASE
        WHEN i.quantity_on_hand = 0 THEN 'out_of_stock'
        WHEN i.quantity_on_hand <= i.reorder_point THEN 'reorder'
        ELSE 'ok'
    END AS stock_status,
    i.updated_at
FROM inventory i
JOIN warehouses w      ON w.warehouse_id = i.warehouse_id
JOIN products   p      ON p.product_id   = i.product_id
JOIN product_categories c ON c.category_id = p.category_id;

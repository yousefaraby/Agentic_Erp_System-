# Synthetic ERP Dataset Generator

Generate a realistic, internally consistent, fully **synthetic** dataset for a
multi-branch agentic ERP system, store it in PostgreSQL, and validate it.

> **Disclaimer:** every record produced by this project is synthetic and randomly
> generated for testing. Nothing represents a real company, person, or
> transaction. Company, supplier, and customer names are fabricated.

- Backend: **Python 3.11+**
- Database: **PostgreSQL 14+** (16 recommended)
- Deterministic, reproducible output via a single random seed
- Configurable record counts
- Full automated validation with a pass/fail report

---

## 1. What it produces

A fictional company, **Meridian Retail & Distribution Group**, with one
headquarters, five branches, three warehouses (one central), suppliers,
customers and 12 months of transactional history.

Default record counts (`seed = 20240101`):

| Table | Rows | Table | Rows |
|---|---:|---|---:|
| roles | 7 | sales_orders | 10,000 |
| departments | 6 | branch_warehouse_services | 10 |
| permissions | 19 | sales_order_items | 40,459 |
| role_permissions | 57 | payments | 9,505 |
| branches | 6 | payment_refunds | 434 |
| warehouses | 3 | shipments | 9,407 |
| employees | 120 | returns | 434 |
| users | 37 | return_items | 985 |
| product_categories | 40 | inventory | 1,398 |
| products | 500 | inventory_reservations | 68 |
| suppliers | 20 | inventory_reservation_items | 238 |
| supplier_products | 1,308 | stock_movements | 46,917 |
| customers | 300 | stock_transfers | 200 |
| | | stock_transfer_items | 410 |
| | | purchase_orders | 500 |
| | | purchase_order_items | 8,621 |
| | | goods_receipts | 460 |
| | | goods_receipt_items | 7,950 |

**Corrected output:** the repaired CSV set is in `out_repaired/`. **Validation:** run `python validate_generated_data.py --directory out_repaired` to
check generated CSV headers, keys, foreign keys, business links, financial
arithmetic and inventory reconciliation before loading the data.

---

## 2. Project structure

```text
erp_data_generator/
├── config.py                 # central, env-overridable configuration
├── db.py                     # PostgreSQL connection + schema creation
├── schema.sql                # complete normalized schema (31 tables + view)
├── load_data.py              # generation pipeline + COPY loader (CLI)
├── validate_data.py          # automated validation + report (CLI)
├── validate_generated_data.py # offline CSV validation (no database needed)
├── generators/
│   ├── base.py               # World context, RNG, money helpers
│   ├── organizational.py     # branches, warehouses, employees, roles, users
│   ├── products.py           # categories, products (+ demand attributes)
│   ├── suppliers.py          # suppliers, supplier-specific pricing/lead times
│   ├── customers.py          # customers
│   ├── sales.py              # sales demand + post-ledger records
│   ├── procurement.py        # purchase orders, receipts
│   └── inventory.py          # assortment, transfers, the inventory ledger
├── queries/
│   ├── sales_agent.sql
│   ├── inventory_agent.sql
│   └── procurement_agent.sql
├── docs/
│   └── DATA_DICTIONARY.md
├── docker-compose.yml        # local PostgreSQL
├── requirements.txt
├── .env.example
└── README.md
```

---

## Database foundation

The verified PostgreSQL implementation is in [database/README.md](database/README.md). It includes the SQLAlchemy mappings, Alembic migrations, Docker setup, repeatable importer, provenance records, reconciliation checks, and integration tests. The architecture and workflow boundaries are documented in [docs/SOFTWARE_ARCHITECTURE_TECHNICAL_DESIGN.md](docs/SOFTWARE_ARCHITECTURE_TECHNICAL_DESIGN.md).

Generated CSVs are intentionally not stored in Git. Generate the corrected dataset locally first, then follow the database README to migrate, import, and verify it:

~~~powershell
python load_data.py --generate-only
docker compose -f database/compose.yml up -d db
docker compose -f database/compose.yml build app
docker compose -f database/compose.yml run --rm app alembic -c database/alembic.ini upgrade head
docker compose -f database/compose.yml run --rm app python -m database.import_data /data
~~~

The database foundation is implemented and verified. The API, approval-file round trip, and AI agents remain planned work.

---

## 3. Quick start

### 3.1 Start PostgreSQL locally (Docker)

```powershell
docker compose up -d
docker compose ps          # wait until postgres is "healthy"
```

This creates a database `erp` with user/password `erp` / `erp` (local dev only,
defined in `docker-compose.yml`). Use any PostgreSQL instance instead if you
prefer: create an `erp` database and point `.env` at it.

### 3.2 Install Python dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3.3 Configure

Copy the example environment file and edit it:

```powershell
Copy-Item .env.example .env
```

`.env` is git-ignored. **Never commit real credentials.** Database passwords are
read only from the environment (`PG*` variables or a `DATABASE_URL`); none are
hardcoded in the source.

### 3.4 Generate, load and validate

```powershell
.\.venv\Scripts\python.exe load_data.py
```

This will:

1. build the whole dataset in memory in dependency order,
2. drop & recreate the `erp` schema (idempotent **reset** — rerunnable),
3. bulk-load every table with `COPY` inside a single transaction,
4. run the validation report and exit non-zero if any essential check fails.

Other entry points:

```powershell
# validate a loaded PostgreSQL database
.\.venv\Scripts\python.exe validate_data.py

# validate CSV output without connecting to a database
.\.venv\Scripts\python.exe validate_generated_data.py

# build the dataset and write CSV files to ./out_repaired (no database required)
.\.venv\Scripts\python.exe load_data.py --generate-only

# write CSV output to a different directory
.\.venv\Scripts\python.exe load_data.py --generate-only --output-dir out_custom

# skip validation
.\.venv\Scripts\python.exe load_data.py --no-validate
```

The CSV validation report is written to `out_repaired/validation_report.txt`;
the database validator writes to `out/validation_report.txt`.

---

## 4. Configuration

All sizes and behaviour are controlled from `config.py` and can be overridden by
environment variables (see `.env.example`). Nothing needs editing in the Python
source to rescale the dataset.

| Variable | Default | Meaning |
|---|---|---|
| `ERP_SEED` | `20240101` | global random seed (reproducibility) |
| `ERP_N_BRANCHES` | `5` | branches (plus 1 HQ) |
| `ERP_N_WAREHOUSES` | `3` | warehouses (1 central + rest regional) |
| `ERP_N_EMPLOYEES` | `120` | employees |
| `ERP_N_SUPPLIERS` | `20` | suppliers |
| `ERP_N_PRODUCTS` | `500` | products |
| `ERP_N_CUSTOMERS` | `300` | customers |
| `ERP_N_SALES_ORDERS` | `10000` | sales orders |
| `ERP_MAX_ITEMS_PER_ORDER` | `10` | max line items per sales order |
| `ERP_N_PURCHASE_ORDERS` | `500` | purchase orders |
| `ERP_N_STOCK_TRANSFERS` | `200` | stock transfer requests |
| `ERP_HISTORY_START` / `ERP_HISTORY_END` | `2023-01-01` / `2023-12-31` | history window |
| `ERP_TAX_RATE` | `0.08` | sales tax rate |
| `ERP_CANCEL_RATE` | `0.04` | share of cancelled sales orders |
| `ERP_RETURN_RATE` | `0.06` | share of delivered orders that are returned |
| `ERP_DELAYED_SHIPMENT_RATE` | `0.08` | share of deliveries that are late |
| `ERP_PARTIAL_PO_RATE` | `0.15` | share of POs only partially received |
| `ERP_DELAYED_PO_RATE` | `0.12` | supplier delay tendency |

**Reproducibility:** the same seed + same counts always yields byte-for-byte
identical data. Changing the seed regenerates an entirely new but still
consistent dataset.

---

## 5. Schema overview and relationships

31 tables in the `erp` schema, grouped by domain:

- **Organization:** `branches`, `warehouses`, `branch_warehouse_services`,
  `departments`, `employees`, `roles`,
  `permissions`, `role_permissions`, `users`
- **Catalog & suppliers:** `product_categories`, `products`, `suppliers`,
  `supplier_products`
- **Customers:** `customers`
- **Sales:** `sales_orders`, `sales_order_items`, `payments`, `shipments`,
  `returns`, `return_items`, `payment_refunds`
- **Inventory:** `inventory`, `stock_movements`, `stock_transfers`,
  `inventory_reservations`, `inventory_reservation_items`, `stock_transfer_items`
- **Procurement:** `purchase_orders`, `purchase_order_items`,
  `goods_receipts`, `goods_receipt_items`

Key relationships (see `docs/DATA_DICTIONARY.md` for the full dictionary):

- Every sales order belongs to exactly one **branch** and one **customer**.
- Sales order items reference a **product** and the **warehouse** used to
  fulfil the order.
- `inventory` is unique per `(warehouse, product)` and is the *current* balance.
- `stock_movements` is an append-only signed ledger; for every
  `(warehouse, product)`, the sum of `quantity_change` equals
  `inventory.quantity_on_hand`.
- `stock_transfers` connect a source and destination **warehouse** (never the
  same one) with a status and lifecycle timestamps.
- `supplier_products` gives supplier-specific price and lead time per product.
- `goods_receipts` / `goods_receipt_items` record what was actually delivered
  against `purchase_order_items`.

A convenience view `v_inventory_position` exposes available-to-promise stock and
a `stock_status` (`ok` / `reorder` / `out_of_stock`).

---

## 6. Inventory accounting rules

The generator simulates a single chronological ledger (see
`generators/inventory.py`). This is what guarantees consistency.

1. **Opening balances** are seeded as `initial` movements on
   `history_start − 1 day` for every `(warehouse, product)` in the warehouse's
   assortment.
2. **Purchase receipts** increase stock at the receipt timestamp, traced to the
   `goods_receipt`.
3. **Sales** consume stock at dispatch time. If less stock than requested is
   available, the item is partially or fully unfulfilled (a backorder) and the
   order status becomes `pending`/`reserved`/`dispatched` accordingly.
4. **Completed transfers** decrease the source and increase the destination.
   Small in-transit losses are modelled as `quantity_shipped > quantity_received`
   and are reflected in the destination balance.
5. **Customer returns** with disposition `restock` add stock back, traced to the
   `return`.
6. Balances never go negative. Every non-opening movement references its
   originating transaction (`reference_type`, `reference_id`).

### Status transitions

**Sales order:** `draft → pending → reserved → dispatched → delivered`, or
`→ cancelled` from any pre-delivery state. A cancelled order always has
`cancelled_at` set; a delivered order always has a delivered shipment.

**Purchase order:** `draft → pending_approval → approved → ordered →
partially_received → received`, or `→ cancelled`. `pending_approval` orders have
no goods receipt; `received`/`partially_received` orders always do.

**Stock transfer:** `pending → approved → in_transit → completed`, or
`→ rejected` / `→ cancelled`. Only `completed` transfers move stock; feasibility
is checked against source stock at dispatch, so unrealistic requests are
rejected.

---

## 7. Built-in business scenarios

The data deliberately contains traceable scenarios for agent testing:

- **Sales:** high-volume products, seasonal peaks (summer / winter / holiday /
  back-to-school), branch-to-branch performance differences, weekday vs weekend
  patterns, cancelled orders, delayed shipments, product returns.
- **Inventory:** products at/below reorder point, out-of-stock pairs,
  warehouses with excess inventory, low-turnover stock, unfulfilled demand
  (stockouts), products stocked at one warehouse but not another.
- **Procurement:** suppliers with different prices and lead times, delayed
  supplier deliveries, partially-received purchase orders, single-sourced
  products (supply risk), purchase orders awaiting approval.
- **Transfers:** completed, pending, rejected, and cancelled transfer requests,
  with enough history to judge whether a transfer recommendation was reasonable.

A snapshot from the corrected default dataset (`seed=20240101`):

- 89.5% order-line unit fill rate; 125 orders wholly unfulfilled
- 129 `(warehouse, product)` pairs at zero stock, 1,005 at/under reorder point,
  84 above three times the reorder point
- 583 late deliveries
- 200 transfers: 124 completed, 25 rejected, 31 pending, 15 cancelled,
  5 approved
- 68 open orders have explicit active stock reservations; 434 returns have
  completed refund transactions

---

## 8. Validation

`validate_data.py` checks a loaded PostgreSQL database. `validate_generated_data.py`
checks generated CSVs offline and writes the validation report into the selected
CSV directory. The offline validator covers headers, row presence, primary and
unique keys, declared foreign keys, branch/warehouse and employee relationships,
order/PO arithmetic, receipts, payments/refunds, reservations and stock ledger
reconciliation.

`validate_data.py` checks a loaded PostgreSQL database with:

- row counts for every table;
- foreign-key integrity (explicit orphan checks);
- duplicate business identifiers;
- sales and purchase order total calculations;
- inventory balance reconciliation vs. the movement ledger;
- completed transfer reconciliation and "pending transfers did not move stock";
- traceability of every stock movement to its transaction;
- business status rules (cancelled/delivered/received/approval states);
- chronological consistency (receipts, shipments, transfers, returns, payments);
- stockout / low-stock / surplus detection;
- branch–warehouse–order associations.

The report lists **passed** and **failed** checks with explanations and ends with
`DATASET VALID: YES/NO`. A dataset is only marked valid when **all essential
checks pass**; the CLI returns a non-zero exit code otherwise.

---

## 9. Agent readiness

`queries/` contains ready-to-run deterministic SQL for the future agents:

- `sales_agent.sql` — top products by revenue, best branch, daily/weekly
  patterns, return rates, monthly trend.
- `inventory_agent.sql` — out-of-stock products, surplus warehouses,
  potential shortages, transfer candidates, stock position, low turnover.
- `procurement_agent.sql` — replenishment needs, best supplier by price/lead
  time, supplier delay rates, POs needing attention, single-source risk.

The generator itself never calls an LLM and has no LLM dependency; agents are
expected to query the resulting database directly.

---

## 10. Reset and cleanup

- **Reset data:** just re-run `python load_data.py`. It drops and recreates the
  `erp` schema every time (idempotent).
- **Stop the database:** `docker compose down`.
- **Remove the database volume:** `docker compose down -v` (deletes all data).

---

## 11. Notes and assumptions

- Monetary math uses Python `Decimal` and PostgreSQL `NUMERIC(14,2)`; totals are
  rounded half-up per line and summed, and the validator recomputes them the
  same way.
- The transfer `in_transit` status is supported by the schema but is not
  generated by default, so that inventory reconciliation stays exact; a small
  in-transit loss is modelled instead on a fraction of completed transfers.
- `password_hash` values are obvious non-functional placeholders
  (`$2b$12$SYNTHETIC...`), never real credentials.
"# Agentic-ERP-System"  git init git add README.md git commit -m "first commit" git branch -M main git remote add origin https://github.com/ZiadKhedr/Agentic-ERP-System.git git push -u origin main

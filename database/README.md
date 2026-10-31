# PostgreSQL foundation

This directory implements the 31 corrected ERP tables plus three provenance tables.
The existing generator and source CSVs remain independent. The application
container uses Python 3.12 and the database uses PostgreSQL 16. Local credentials
in compose.yml are for isolated development only.

## Run with Docker

From the repository root, with Docker Desktop running:

```powershell
docker compose -f database/compose.yml up -d db
docker compose -f database/compose.yml build app
docker compose -f database/compose.yml run --rm app alembic -c database/alembic.ini upgrade head
docker compose -f database/compose.yml run --rm app python -m database.import_data /data
docker compose -f database/compose.yml run --rm app python -m pytest database/tests -v
```

The separate database is exposed on localhost:55432. Source CSVs are mounted
read-only at /data. The named volume is independent of the old generator's
PostgreSQL volume. Re-run the import command to exercise its no-op behavior.
`docker compose -f database/compose.yml down` stops containers and preserves data.
Do not add `-v` unless intentionally deleting the database.

## Native Python 3.12 installation

```powershell
python3.12 -m venv .venv-db
.\.venv-db\Scripts\python.exe -m pip install -r database/requirements.txt
$env:ERP_DATABASE_URL = 'postgresql+psycopg://erp:local-development-only@localhost:55432/erp_foundation'
$env:ERP_DATA_DIR = 'out_repaired'
.\.venv-db\Scripts\alembic.exe -c database/alembic.ini upgrade head
.\.venv-db\Scripts\python.exe -m database.import_data out_repaired
.\.venv-db\Scripts\python.exe -m pytest database/tests -v
```

Use your actual Python 3.12 executable if its launcher name differs.
Dependencies are bounded in requirements.txt; exact dependency locking should
follow the first successful build in the deployment environment.

## Design and operation

`migrations/versions/0001_schema.sql` is an immutable snapshot of the corrected
schema, plus import provenance and additional accounting/branch checks. It
contains no reset statement. Alembic records the revision and applies it once.
Do not edit an applied migration: add a new revision. Automatic destructive
downgrade is disabled.

`models.py` declares one ORM class per ERP table using SQLAlchemy deferred
reflection. Call `prepare_models(engine)` after migration; ORM metadata then
contains the actual PostgreSQL types, foreign keys, checks and indexes. Example:

```python
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from database.models import prepare_models, SalesOrders
engine = create_engine(database_url)
prepare_models(engine)
with Session(engine) as session:
    orders = session.scalars(select(SalesOrders).limit(10)).all()
```

SQL migrations are the schema authority; Alembic autogeneration is intentionally
not configured for reflected models. Foreign keys preserve all branches,
warehouses and normalized departments. The branch/warehouse service table
controls operational coverage. Order employee/branch pairs are constrained by
a composite foreign key. Stock balances and reservations remain projections
that are reconciled against transaction rows. Exact money uses NUMERIC and dates
use DATE/TIMESTAMPTZ. The migrated inventory-position view exposes available stock.

The importer reads and verifies each CSV against its manifest, checks headers
and counts, and holds a transaction-level advisory lock. All COPY operations,
provenance inserts and reconciliation run in one transaction. A failure rolls
back the entire import. The same manifest is a no-op after reconciliation;
a different dataset is rejected in a nonempty database. CSV IDs are retained. Single-column integer keys use PostgreSQL identity defaults; the importer advances their sequences after COPY so future inserts do not collide.
A batch records the complete manifest and source path; each file records hash,
table and count. Because this importer accepts one snapshot into an empty
schema, each imported primary key is traceable to that batch's original CSV.

Database constraints enforce row-local validity and declared foreign keys.
`integrity.py` additionally reconciles source counts, branch service areas,
stock ledger sequences, reservations, receipts, order/PO totals, returns and
refunds. These cross-table checks are an import gate; future application write
services must enforce them transactionally as well. This phase does not add
users/authentication enforcement, agents, accounting journals or payroll data.

## Schema reference

Read the frozen migration for exact columns, types, defaults, keys, constraints,
indexes and the view. `../docs/DATA_DICTIONARY.md` describes the source entities.
Provenance has `import_batches(batch_id UUID PK, dataset_hash TEXT UNIQUE,
source_path TEXT, manifest JSONB, imported_at TIMESTAMPTZ)` and
`import_files(batch_id FK, table_name TEXT, sha256 TEXT, row_count BIGINT)` with
composite primary key `(batch_id,table_name)`.

```mermaid
erDiagram
  BRANCHES ||--o{ EMPLOYEES : employs
  DEPARTMENTS ||--o{ EMPLOYEES : groups
  BRANCHES ||--o{ BRANCH_WAREHOUSE_SERVICES : served_by
  WAREHOUSES ||--o{ BRANCH_WAREHOUSE_SERVICES : serves
  CUSTOMERS ||--o{ SALES_ORDERS : orders
  SALES_ORDERS ||--|{ SALES_ORDER_ITEMS : contains
  PRODUCTS ||--o{ SALES_ORDER_ITEMS : sold
  SALES_ORDERS ||--o{ INVENTORY_RESERVATIONS : reserves
  INVENTORY_RESERVATIONS ||--|{ INVENTORY_RESERVATION_ITEMS : allocates
  WAREHOUSES ||--o{ INVENTORY : holds
  PRODUCTS ||--o{ INVENTORY : stocked
  WAREHOUSES ||--o{ STOCK_MOVEMENTS : records
  STOCK_TRANSFERS ||--|{ STOCK_TRANSFER_ITEMS : moves
  SUPPLIERS ||--o{ PURCHASE_ORDERS : supplies
  PURCHASE_ORDERS ||--|{ PURCHASE_ORDER_ITEMS : orders
  PURCHASE_ORDERS ||--o{ GOODS_RECEIPTS : receives
  GOODS_RECEIPTS ||--|{ GOODS_RECEIPT_ITEMS : contains
  SALES_ORDERS ||--o{ PAYMENTS : collects
  SALES_ORDERS ||--o{ SHIPMENTS : dispatches
  SALES_ORDERS ||--o{ RETURNS : returns
  RETURNS ||--|{ RETURN_ITEMS : contains
  RETURNS ||--o{ PAYMENT_REFUNDS : settles
  PAYMENTS ||--o{ PAYMENT_REFUNDS : refunds
  IMPORT_BATCHES ||--|{ IMPORT_FILES : proves
```

## Backup and restore verification

From the host repository directory, after import succeeds:

```powershell
python -m database.backup_verify
```

This writes a PostgreSQL custom-format backup to `database/backups/`, restores
it into a uniquely named temporary database, reconciles its data and provenance,
and removes the temporary database. It preserves the running foundation database.
The backup file remains available for recovery and is ignored by Git.

## Ledger-only inventory correction

Live PostgreSQL validation exposed 14 stock pairs (99 units) that are present
in the source movement ledger but absent from its inventory CSV. Migration
0002 adds `provenance.derived_inventory_rows`. During initial import these pairs
are inserted using their complete movement sums and product reorder settings;
source CSVs are preserved. Their synthetic count date inherits the snapshot
count date and is explicitly recorded as derived, not a new physical count.
Source file counts remain unchanged in `import_files`; reconciliation expects
source inventory rows plus the recorded derived rows. The integration suite
verifies exact correspondence to ledger-only source pairs.

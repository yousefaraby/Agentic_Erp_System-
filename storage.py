"""SQLite schema, workbook import, source records and workflow state."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from openpyxl import load_workbook


DEFAULT_WORKBOOK = Path(__file__).parent / "database" / "Cedarline_ERP.xlsx"


def load_tables(
    *table_names: str, workbook_path: str | Path = DEFAULT_WORKBOOK
) -> dict[str, list[dict[str, object]]]:
    """Read one or more snake_case table names from the saved Excel file."""
    with Path(workbook_path).open("rb") as source:
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            tables = {}
            for name in table_names:
                sheet_name = name.replace("_", " ").title()
                if sheet_name == "Start Here" or sheet_name not in workbook.sheetnames:
                    raise ValueError(f"Data sheet '{sheet_name}' is missing from {workbook_path}.")

                rows = workbook[sheet_name].values
                header = list(next(rows, ()))
                while header and header[-1] is None:
                    header.pop()
                if (
                    not header
                    or any(not isinstance(column, str) or not column.strip() for column in header)
                    or len(set(header)) != len(header)
                ):
                    raise ValueError(f"'{sheet_name}' must have unique column names in row 1.")

                records = []
                for row_number, row in enumerate(rows, start=2):
                    if any(value is not None for value in row[len(header):]):
                        raise ValueError(f"'{sheet_name}' row {row_number} has data without a header.")
                    values = row[:len(header)]
                    if any(value is not None for value in values):
                        records.append(dict(zip(header, values, strict=True)))
                tables[name] = records
            return tables
        finally:
            workbook.close()


SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, unit TEXT NOT NULL,
    unit_price_minor INTEGER NOT NULL, active INTEGER NOT NULL
);
CREATE TABLE branches (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, active INTEGER NOT NULL);
CREATE TABLE warehouses (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL);
CREATE TABLE branch_warehouses (
    branch_id INTEGER REFERENCES branches, warehouse_id INTEGER REFERENCES warehouses,
    is_primary INTEGER NOT NULL, PRIMARY KEY (branch_id, warehouse_id)
);
CREATE TABLE inventory (
    warehouse_id INTEGER REFERENCES warehouses, product_id INTEGER REFERENCES products,
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL CHECK (reserved BETWEEN 0 AND on_hand),
    PRIMARY KEY (warehouse_id, product_id)
);
CREATE TABLE sales_orders (
    id INTEGER PRIMARY KEY, order_number TEXT UNIQUE, customer_id INTEGER NOT NULL REFERENCES customers,
    branch_id INTEGER NOT NULL REFERENCES branches, status TEXT NOT NULL, channel TEXT NOT NULL,
    total_minor INTEGER NOT NULL, currency TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE sales_order_items (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES sales_orders,
    product_id INTEGER NOT NULL REFERENCES products, quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_minor INTEGER NOT NULL
);
CREATE TABLE inventory_reservations (
    id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES sales_orders,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses, UNIQUE (order_id, warehouse_id)
);
CREATE TABLE inventory_reservation_items (
    reservation_id INTEGER REFERENCES inventory_reservations,
    order_item_id INTEGER REFERENCES sales_order_items, quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (reservation_id, order_item_id)
);
CREATE TABLE email_intake (
    message_key TEXT PRIMARY KEY, source TEXT NOT NULL, sender TEXT NOT NULL,
    intent TEXT, result TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE inbox_cursor (
    mailbox TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
    PRIMARY KEY (mailbox, uidvalidity)
);
"""


def normalize(value: object) -> str:
    return " ".join(str(value if value is not None else "").split()).casefold()


def connect(path, *, initialize=False):
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not initialize and not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_intake'"
    ).fetchone():
        connection.close()
        raise ValueError("Initialize the order database with the init command first.")
    return connection


def minor_units(amount) -> int:
    return int(round(Decimal(str(amount)) * 100))


def initialize(path, workbook):
    tables = load_tables(
        "customers", "products", "branches", "warehouses", "branch_warehouse_services",
        "inventory", "sales_orders", "sales_order_items", workbook_path=workbook,
    )
    with closing(connect(path, initialize=True)) as db, db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
            raise ValueError("Use a new SQLite database path for init.")
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", (
            (r["customer_id"], normalize(r["email"]) or None,
             r["company_name"] or f'{r["first_name"] or ""} {r["last_name"] or ""}'.strip(),
             bool(r["is_active"]))
            for r in tables["customers"]
        ))
        db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", (
            (r["product_id"], normalize(r["sku"]).upper(), r["unit_of_measure"].upper(),
             minor_units(r["list_price"]), bool(r["is_active"]))
            for r in tables["products"]
        ))
        db.executemany("INSERT INTO branches VALUES (?, ?, ?)", (
            (r["branch_id"], r["branch_code"].upper(), bool(r["is_active"]))
            for r in tables["branches"]
        ))
        db.executemany("INSERT INTO warehouses VALUES (?, ?)", (
            (r["warehouse_id"], r["warehouse_code"].upper())
            for r in tables["warehouses"] if r["is_active"]
        ))
        active_branches = {row[0] for row in db.execute("SELECT id FROM branches WHERE active = 1")}
        active_warehouses = {row[0] for row in db.execute("SELECT id FROM warehouses")}
        db.executemany("INSERT INTO branch_warehouses VALUES (?, ?, ?)", (
            (r["branch_id"], r["warehouse_id"], bool(r["is_primary"]))
            for r in tables["branch_warehouse_services"]
            if r["can_fulfill"] and r["branch_id"] in active_branches
            and r["warehouse_id"] in active_warehouses
        ))
        db.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?)", (
            (r["warehouse_id"], r["product_id"], r["quantity_on_hand"], r["quantity_reserved"])
            for r in tables["inventory"] if r["warehouse_id"] in active_warehouses
        ))
        # Import the order history for reference and keep the workbook's IDs.
        db.executemany("INSERT INTO sales_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            (r["sales_order_id"], r["order_number"], r["customer_id"], r["branch_id"], r["status"],
             r["channel"], minor_units(r["total_amount"]), r["currency"], r["created_at"].isoformat())
            for r in tables["sales_orders"]
        ))
        db.executemany("INSERT INTO sales_order_items VALUES (?, ?, ?, ?, ?)", (
            (r["sales_order_item_id"], r["sales_order_id"], r["product_id"], r["quantity"],
             minor_units(r["unit_price"]))
            for r in tables["sales_order_items"]
        ))


SOURCE_TABLES = (
    "sales_orders", "sales_order_items", "customers", "shipments", "returns", "return_items",
    "inventory", "products", "product_categories", "warehouses", "stock_movements",
    "stock_transfers", "stock_transfer_items", "inventory_reservations",
    "inventory_reservation_items", "purchase_orders", "purchase_order_items", "suppliers",
    "supplier_products", "goods_receipts", "goods_receipt_items", "payments", "payment_refunds",
    "branches", "branch_warehouse_services", "employees", "trucks", "truck_trips", "truck_loads",
)

WORKFLOW_SCHEMA = """
CREATE TABLE company_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_data (
    table_name TEXT NOT NULL, row_number INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (table_name, row_number)
);
CREATE TABLE company_events (
    event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'planned', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES company_events,
    agent TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
    summary TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executed', 'rejected', 'blocked')),
    decision TEXT CHECK (decision IN ('approve', 'reject')),
    decided_at TEXT, result TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_key, kind, target)
);
CREATE TABLE agent_activity (
    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL REFERENCES company_events,
    agent TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
    trace_id TEXT, note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE outbound_messages (
    approval_id INTEGER PRIMARY KEY REFERENCES approvals,
    channel TEXT NOT NULL, recipient TEXT NOT NULL, message TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('simulated', 'queued', 'sending', 'accepted', 'failed', 'uncertain')),
    provider_reference TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE demo_shipments (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES sales_orders,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses,
    truck TEXT, destination TEXT NOT NULL, weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    status TEXT NOT NULL CHECK (status IN ('in_transit', 'delivered')),
    approval_id INTEGER NOT NULL REFERENCES approvals,
    UNIQUE (order_id, warehouse_id)
);
CREATE TABLE demo_replenishments (
    id INTEGER PRIMARY KEY REFERENCES approvals,
    order_id INTEGER NOT NULL REFERENCES sales_orders, product_id INTEGER NOT NULL REFERENCES products,
    method TEXT NOT NULL CHECK (method IN ('transfer', 'purchase')),
    destination_id INTEGER NOT NULL REFERENCES warehouses, origin_id INTEGER REFERENCES warehouses,
    supplier_id INTEGER, quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL CHECK (status IN ('in_transit', 'ordered', 'received'))
);
CREATE UNIQUE INDEX one_open_replenishment_per_item
ON demo_replenishments (order_id, product_id) WHERE status <> 'received';
CREATE TABLE demo_refunds (
    id INTEGER PRIMARY KEY REFERENCES approvals, return_reference TEXT NOT NULL UNIQUE,
    payment_id INTEGER NOT NULL, order_item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0)
);
CREATE TABLE work_roster (
    employee_id INTEGER NOT NULL, work_date TEXT NOT NULL, minutes INTEGER NOT NULL CHECK (minutes BETWEEN 0 AND 1440),
    approval_id INTEGER NOT NULL REFERENCES approvals,
    PRIMARY KEY (employee_id, work_date)
);
CREATE TABLE payroll_ledger (
    period_start TEXT NOT NULL, employee_id INTEGER NOT NULL, minutes INTEGER NOT NULL,
    gross_minor INTEGER NOT NULL CHECK (gross_minor >= 0),
    approval_id INTEGER NOT NULL REFERENCES approvals,
    PRIMARY KEY (period_start, employee_id)
);
CREATE TABLE demo_pay_rates (
    employee_id INTEGER PRIMARY KEY, hourly_minor INTEGER NOT NULL CHECK (hourly_minor > 0)
);
CREATE TABLE demo_attendance (
    employee_id INTEGER NOT NULL REFERENCES demo_pay_rates, work_date TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK (minutes BETWEEN 0 AND 1440),
    PRIMARY KEY (employee_id, work_date)
);
"""


@dataclass(frozen=True)
class Draft:
    kind: str
    target: str
    summary: str
    payload: dict


@dataclass(frozen=True)
class Assessment:
    summary: str
    drafts: tuple[Draft, ...] = ()


class Blocked(ValueError):
    """The approved proposal no longer matches the current facts."""


def encode(value) -> str:
    def convert(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"Cannot serialize {type(item).__name__}")

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=convert, allow_nan=False)


@contextmanager
def transaction(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        yield


STOCK_REPLENISHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_replenishments (
    id INTEGER PRIMARY KEY REFERENCES approvals(id),
    destination_id INTEGER NOT NULL REFERENCES warehouses(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL CHECK (status IN ('inbound', 'received'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_stock_replenishment
ON stock_replenishments (destination_id, product_id) WHERE status = 'inbound';
"""


def bootstrap(path, workbook):
    """Populate the company tables once, including all Cedarline source tables."""
    if not path.exists() or not path.stat().st_size:
        initialize(path, workbook)
    with closing(connect(path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'company_meta'").fetchone():
            db.executescript(STOCK_REPLENISHMENT_SCHEMA)
            return
        data = load_tables(*SOURCE_TABLES, workbook_path=workbook)
        db.execute("PRAGMA journal_mode = WAL")
        with db:
            db.executescript("BEGIN IMMEDIATE;\n" + WORKFLOW_SCHEMA + STOCK_REPLENISHMENT_SCHEMA)
            db.executemany("INSERT INTO source_data VALUES (?, ?, ?)", (
                (table, number, encode(row))
                for table, rows in data.items() for number, row in enumerate(rows, start=2)
            ))
            db.execute("INSERT INTO company_meta VALUES ('source_workbook', ?)", (workbook.name,))


def source(db, name):
    return [json.loads(row[0]) for row in db.execute(
        "SELECT payload FROM source_data WHERE table_name = ? ORDER BY row_number", (name,),
    )]


def enqueue(db, event_key, kind, payload):
    db.execute("""
        INSERT INTO company_events (event_key, kind, payload) VALUES (?, ?, ?)
        ON CONFLICT (event_key) DO NOTHING
    """, (event_key, kind, encode(payload)))


def synchronize_email(db):
    """Queue new intake events and backfill order confirmations for saved bookings."""
    with transaction(db):
        for row in db.execute("""
            SELECT i.rowid, i.sender, i.result
            FROM email_intake AS i
            LEFT JOIN company_events AS e ON e.event_key = 'email:' || i.rowid
            LEFT JOIN company_events AS c ON c.event_key = 'order-confirmation:' || i.rowid
            WHERE e.event_key IS NULL OR (
                c.event_key IS NULL AND json_extract(i.result, '$.status') IN ('reserved', 'awaiting_stock')
            )
            ORDER BY i.rowid
        """):
            result = json.loads(row["result"])
            facts = {"sender": row["sender"], "intake": result}
            enqueue(db, f'email:{row["rowid"]}', "email.received", facts)
            if result["status"] in {"reserved", "awaiting_stock"}:
                enqueue(db, f'order-confirmation:{row["rowid"]}', "order.confirmation_requested", facts)


def log(db, event_key, agent, status, summary, trace_id=None, note=None):
    db.execute("""
        INSERT INTO agent_activity (event_key, agent, status, summary, trace_id, note)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (event_key, agent, status, summary, trace_id, note))


def save_outbound(db, approval_id, channel, recipient, message):
    status = "simulated"
    db.execute("""
        INSERT INTO outbound_messages (approval_id, channel, recipient, message, status)
        VALUES (?, ?, ?, ?, ?)
    """, (approval_id, channel, recipient, message, status))
    return {"channel": channel, "recipient": recipient, "delivery": status}


def find_order(db, number):
    return db.execute("SELECT * FROM sales_orders WHERE order_number = ?", (number,)).fetchone()


def order_lines(db, order_id):
    return db.execute("""
        SELECT p.*, i.id AS item_id, i.quantity
        FROM sales_order_items AS i JOIN products AS p ON p.id = i.product_id
        WHERE i.order_id = ? ORDER BY i.id
    """, (order_id,)).fetchall()

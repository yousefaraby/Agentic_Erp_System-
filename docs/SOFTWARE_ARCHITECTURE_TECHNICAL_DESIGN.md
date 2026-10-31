# Agentic ERP — Software Architecture and Technical Design

**Document status:** implementation design; system foundation is implemented, application and agents are not.

**Audience:** five-person development team, reviewers, and future operators.

**Scope:** multi-branch, multi-warehouse sales, inventory, procurement, returns, manager approval by workbook, and a future orchestrated agent layer.

**Last reviewed:** 2026-10-28.

## 1. Purpose and implementation status

This document describes the target system and how to build it. It distinguishes verified code from proposed components so the team does not confuse architecture with completed implementation.

| Area | Status | Evidence / notes |
|---|---|---|
| Corrected synthetic source dataset | Implemented and verified | 31 source tables in `out_repaired/`; original source files retained. |
| PostgreSQL schema, Alembic migrations, SQLAlchemy mappings, Docker database | Implemented | PostgreSQL 16, Python 3.12.16; Alembic revisions `0001` and `0002`. |
| Repeatable atomic data importer and import provenance | Implemented | Manifest/hash/header/count checks; idempotent same-snapshot import. |
| Database reconciliation | Implemented and verified | 45 checks passed after import and backup restore. |
| Automated integration tests | Implemented and verified | 11 tests passed. |
| REST API, file request/approval round trip, runtime orchestration | **Proposed** | Not implemented. |
| AI/LLM agents and model-provider integration | **Proposed** | No agents are implemented. |
| Application authentication, human user accounts, application access control | **Out of MVP scope** | The user selected manager-edited approval files. Existing synthetic `users`, `roles`, and `permissions` tables are imported data only and are not an application login system. |
| General ledger, vendor invoice/payment history, payroll, production | **Out of current data scope** | The dataset does not support these as complete operational/accounting modules. |

Phase 1 verification was performed on 2026-10-28: empty-database migrations, all 31 source-table imports, 45 reconciliations, repeat-import idempotency, 11 tests, and backup/restore reconciliation all passed. The import derives 14 inventory snapshot rows (99 units) from the source stock ledger because those pairs were missing from `inventory.csv`; it records them in `provenance.derived_inventory_rows` without altering source CSVs. See `database/VERIFICATION.md` for the test record.

## 2. Product goals, constraints, and non-goals

### 2.1 Goals

- Support every branch, warehouse, and department in the corrected data, while applying branch/warehouse service mappings to each operational request.
- Provide reliable read-only business facts to specialist agents.
- Let agents return evidence-based summaries and proposed actions, not direct database writes.
- Let a manager review a generated workbook and mark it **Approved**, **Rejected/Cancelled**, or **Needs changes**, with an optional note.
- Import that decision safely and make approved changes through deterministic, idempotent business services.
- Preserve the request, approval-file, agent-run, and source-data provenance needed to explain a decision later.

### 2.2 MVP constraints

- No application login, human user accounts, or role/permission workflow.
- A manager decision is communicated by returning an approval workbook. A typed name is descriptive text, not proof of identity.
- No agent directly calls another agent. The orchestrator routes typed tasks and collects typed results.
- No agent receives a database connection, arbitrary SQL capability, or write tool.
- Start with a modular Python application and synchronous orchestration. Add a broker/worker queue only when measured run duration or throughput requires it.
- AI suggestions are advisory. Exact arithmetic, state transitions, stock allocation, and approval validation are deterministic code.

### 2.3 Explicit non-goals for this phase

No payment processor, email/SMS service, web dashboard, authentication provider, general ledger, payroll, accounts-payable invoice lifecycle, or production/assembly module is specified as delivered. These can be separate phases after their business rules and data are available.

## 3. Architecture overview

Use a **modular monolith**: one Python application with explicit packages for intake, orchestration, agents, database tools, business services, approvals, and audit. PostgreSQL remains the system of record. Separating every agent into a network microservice would add operational complexity before the workflow and message contracts are stable.

~~~mermaid
flowchart LR
  subgraph Inputs[Triggers and inputs]
    WB[Request workbook in inbox]
    SCH[Scheduled scan]
    CLI[Operator CLI / internal API]
  end
  subgraph App[Python 3.12 modular application — proposed]
    IN[Intake and deterministic validation]
    OR[Orchestrator and run coordinator]
    SA[Sales agent]
    IA[Inventory agent]
    PA[Procurement agent]
    RA[Returns agent]
    TOOLS[Allow-listed read-only ERP tools]
    WF[Approval workbook generator/importer]
    TX[Deterministic transaction services]
  end
  DB[(PostgreSQL 16<br/>erp + provenance + proposed workflow schema)]
  FILES[(Controlled inbox / review / archive folders)]
  MAN[Manager edits decision and note]
  WB --> IN
  SCH --> OR
  CLI --> IN
  IN --> OR
  OR --> SA
  OR --> IA
  OR --> PA
  OR --> RA
  SA --> TOOLS
  IA --> TOOLS
  PA --> TOOLS
  RA --> TOOLS
  TOOLS -->|scoped SELECT only| DB
  SA -->|typed result| OR
  IA -->|typed result| OR
  PA -->|typed result| OR
  RA -->|typed result| OR
  OR --> WF
  WF --> FILES
  FILES --> MAN
  MAN --> FILES
  FILES --> WF
  WF -->|valid approval only| TX
  TX -->|transactional writes| DB
  IN -->|request/run/audit records| DB
  OR -->|run/task/audit records| DB
~~~

### 3.1 Component responsibilities

| Component | Responsibility | Database access |
|---|---|---|
| File intake | Detect completed request files; parse and validate workbook structure; assign request ID/revision; compute canonical payload hash. | Writes only request/intake records through repositories. |
| Scheduler | Raise named, bounded triggers such as daily low-stock review. It does not contain business logic. | Calls orchestrator; no direct SQL. |
| Orchestrator | Create a run; classify the request; build task envelopes; dispatch agents; collect, validate and merge results; apply timeouts/error policy; ask for a clarification or create a review file. | Reads/writes run/task state via repository; domain facts go through tools. |
| Specialist agents | Analyze one bounded business domain and return facts/recommendations with source references. | Read-only tool gateway only. |
| Read-only tool gateway | Expose allow-listed, parameterized SQLAlchemy queries with branch/warehouse scoping and bounded result sizes. | SELECT using a restricted DB service role. |
| Workbook generator | Render a review file from a frozen request revision and deterministic facts; clearly separate locked business fields from manager decision fields. | Reads request/review data; writes file metadata. |
| Approval importer | Store the returned file receipt; parse allowed decision fields; verify revision/payload hash and schema; reject tampering, stale files, duplicates and invalid states. | Writes approval/import events through repositories; calls transaction service. |
| Transaction services | Enforce business invariants, concurrency, idempotency, and atomic writes for sales, reservations, POs, transfers, or returns. | Write service role, limited to required ERP tables. |
| PostgreSQL | Canonical transactional state, source-data provenance, and workflow/agent audit state. | Persistent store. |
| File folders | Exchange request and approval workbooks; archive generated/returned artifacts. | Files are not the canonical ERP record. |

The existing `database/compose.yml` is a **foundation/import setup**, not the future API deployment. Its `app` container runs the importer. Do not describe it as an already running ERP API or agent service.

## 4. Trigger and runtime pipeline

### 4.1 Supported trigger types

1. **New request workbook:** an operator places a workbook into the inbox or uploads it to the future internal API. Intake waits until the file is completely written (atomic rename or `.ready` marker), then validates it.
2. **Returned approval workbook:** the manager places the completed review file into the approvals inbox. The importer treats it as untrusted input and checks the request ID, revision, payload hash, allowed decision and allowed editable columns.
3. **Scheduled analysis:** a scheduler asks the orchestrator to produce a low-stock or sales/stock review for a specified date range and branch/warehouse scope. A scheduled run can only create a report/review artifact; it does not execute a purchase order or stock adjustment.
4. **Internal API/CLI trigger:** a future private API or operator command starts a named workflow. It uses the same validation and orchestration path as file intake.

### 4.2 Run state and task state

Suggested run statuses: `queued`, `running`, `succeeded`, `partial`, `needs_input`, `failed`, `cancelled`. Suggested agent task statuses: `queued`, `running`, `succeeded`, `no_data`, `needs_input`, `failed`, `timed_out`.

A run has one `run_id`; every agent handoff has a unique `task_id` and optional `parent_task_id`. Retries keep the logical task identity/idempotency key and increment an attempt number. Store start/end times and short, sanitized errors. Do not rely on conversation history as runtime state.

### 4.3 Request/approval state machine

~~~mermaid
stateDiagram-v2
  [*] --> Received
  Received --> Validating
  Validating --> RejectedInput: malformed / invalid references
  Validating --> ReadyForReview: valid request and analysis complete
  ReadyForReview --> ReviewIssued: workbook generated
  ReviewIssued --> ApprovalReceived: manager returns file
  ApprovalReceived --> NeedsChanges: decision is Needs changes
  NeedsChanges --> NewRevision: requester updates business fields
  NewRevision --> Validating
  ApprovalReceived --> Declined: Rejected or Cancelled
  ApprovalReceived --> ApprovedPendingExecution: Approved and file verified
  ApprovedPendingExecution --> Committed: transactional rules pass
  ApprovedPendingExecution --> ConflictNeedsRevision: stock or reference changed
  ConflictNeedsRevision --> NewRevision
  Committed --> [*]
  Declined --> [*]
  RejectedInput --> [*]
~~~

Each revision is immutable. A business-field change creates a new revision and new canonical payload hash. A decision applies only to the exact revision/hash reviewed. **Needs changes** does not mutate the existing revision.

## 5. Agent architecture and communication

### 5.1 Communication rule

Agents do not freely converse or call each other. The orchestrator sends a typed task to a specialist and receives a typed result. If Sales needs an inventory fact, the orchestrator dispatches an Inventory task and provides its result to Sales or merges both results. This makes scope, retries, audit, and failure handling explicit.

In the MVP, these handoffs can be Python function calls with Pydantic-validated messages. They still use the same envelope that would be placed on a broker later. Do not add Redis/Celery/Kafka until asynchronous workload is justified by measured need.

### 5.2 Task envelope

~~~json
{
  "run_id": "uuid",
  "task_id": "uuid",
  "parent_task_id": null,
  "task_type": "inventory.availability",
  "branch_id": 2,
  "warehouse_id": 3,
  "input_refs": [{"type": "request_revision", "id": "uuid", "revision": 1}],
  "parameters": {"product_ids": [101, 102], "as_of": "2026-10-28"},
  "allowed_tools": ["get_inventory_snapshot", "get_branch_warehouse_map"],
  "deadline_seconds": 30,
  "attempt": 1
}
~~~

The IDs above are examples only. A task must include a bounded intent, valid organizational scope, inputs/references, allowed tool names, and a timeout. Do not put passwords, connection strings, or unrestricted SQL in a task.

### 5.3 Result envelope

~~~json
{
  "run_id": "uuid",
  "task_id": "uuid",
  "agent_name": "inventory",
  "status": "succeeded",
  "facts": [{"product_id": 101, "warehouse_id": 3, "available": 12}],
  "source_refs": [{"table": "erp.inventory", "keys": {"warehouse_id": 3, "product_id": 101}}],
  "recommendation": "Review replenishment for product 101.",
  "assumptions": [],
  "warnings": [],
  "error": null
}
~~~

Validate results against a versioned Pydantic model. Recommendations must distinguish facts from inference and include source references. The orchestrator rejects malformed output, records failure, and either returns a partial result or stops a dependent workflow according to policy.

### 5.4 Agent responsibilities and proposed functions

| Agent | Bounded responsibilities | Example functions | Prohibited behavior |
|---|---|---|---|
| Orchestrator (coordinator, not a domain writer) | Route requests, determine required specialists, merge evidence, create report/review artifact. | `ingest_trigger`, `validate_request`, `classify_intent`, `plan_tasks`, `dispatch_task`, `collect_results`, `merge_results`, `handle_failure`, `create_review_file`. | No raw SQL, no bypass of approval importer, no silent business mutation. |
| Sales | Summarize sales, interpret order request, draft order lines and explain order state. | `summarize_sales`, `draft_order_lines`, `explain_order_status`. | Cannot create/cancel orders or set prices outside deterministic policy. |
| Inventory | Explain availability, reservations, movement history and replenishment risks. | `get_availability`, `calculate_reservations`, `flag_replenishment`. | Cannot adjust on-hand/reserved quantity or create stock movements. |
| Procurement | Compare approved supplier/product terms, open POs and replenishment options; draft request. | `compare_suppliers`, `draft_purchase_request`. | Cannot issue a PO or send it to a supplier. |
| Returns | Summarize return/payment/refund status and prepare return review. | `summarize_return`, `check_refund_status`, `draft_return_review`. | Cannot approve a return or execute a refund. Payment processor integration is outside current scope. |

The five proposed owners can map to these components: (1) orchestrator/contracts, (2) sales agent, (3) inventory agent, (4) procurement agent, (5) returns agent. All five share the same interface contract, tests, and no-direct-write rule. An additional reviewer should verify database tool and approval boundaries across all branches.

### 5.5 Database tool gateway

Expose business-level tools, not generic SQL:

- `get_branch_warehouse_map(branch_id, as_of)`
- `get_product_catalog(sku_or_category, limit)`
- `get_inventory_snapshot(branch_id, warehouse_id, product_ids, as_of)`
- `get_sales_summary(branch_id, date_from, date_to, product_ids)`
- `get_order_status(order_number)`
- `get_open_purchase_orders(warehouse_id, product_ids)`
- `get_supplier_options(product_ids, warehouse_id)`
- `get_return_and_refund_status(return_number)`

Enforce `branch_warehouse_services` validity and `can_fulfill`/`can_transfer` flags in repository queries. Reject missing scope where the operation needs it. Limit date windows, result size, and selected fields. Return stable business IDs plus source references, not entire customer/employee rows.

## 6. Database design

### 6.1 Schemas and source-of-truth boundaries

- **`erp`** — normalized operational data from the corrected 31-table dataset and the `v_inventory_position` view.
- **`provenance`** — immutable source import batches/files and explicit records for derived inventory rows.
- **`workflow` (proposed additive migration)** — request revisions, approval-file receipts/decisions, agent runs/tasks, and optionally an outbox. This schema is not present in the current migration set.

The database is canonical after validation. CSV and Excel workbooks are exchange artifacts. Never hand-edit imported source CSVs to represent an approval.

### 6.2 Current ERP table groups

| Domain | Tables | Key relationships and rules |
|---|---|---|
| Organization | `branches`, `warehouses`, `branch_warehouse_services`, `departments`, `employees`, `roles`, `permissions`, `role_permissions`, `users` | Branches and warehouses are many-to-many over effective-dated service rows. Employees reference branch, optional warehouse, department and optional manager. Composite employee/branch reference protects order branch consistency. `users`/roles/permissions are imported synthetic data, not used for MVP authentication. |
| Product and supplier | `product_categories`, `products`, `suppliers`, `supplier_products` | Categories may be hierarchical; products reference categories; supplier product rows hold supplier-specific cost/lead-time/validity. |
| Customer | `customers` | Sales orders reference customers. Customer type-specific fields are constrained. |
| Sales and fulfillment | `sales_orders`, `sales_order_items`, `payments`, `shipments` | Orders have branch/customer and optional employee; line items reference product and fulfillment warehouse. Line totals and order totals are reconciled. Payments and shipments reference an order. |
| Returns and refunds | `returns`, `return_items`, `payment_refunds` | Returns reference order/customer/warehouse; return lines reference original order lines; refunds reference both payment and return and cannot exceed payment total. |
| Inventory | `inventory`, `inventory_reservations`, `inventory_reservation_items`, `stock_movements` | Inventory is unique per warehouse/product; on-hand and reserved are nonnegative and reserved cannot exceed on-hand. Reservation lines identify order lines and stock pairs. Stock movement ledger is append-oriented and reconciled to inventory. |
| Transfers | `stock_transfers`, `stock_transfer_items` | Transfer header has distinct source/destination warehouses; quantities are bounded from requested through shipped to received. Movements record transfer out/in. |
| Procurement and receiving | `purchase_orders`, `purchase_order_items`, `goods_receipts`, `goods_receipt_items` | PO references supplier/warehouse; item quantities and received totals reconcile to receipt items. Approval/request separation is represented. |
| Projection | `v_inventory_position` | Joins inventory, warehouses, products and categories; available = on-hand minus reserved; exposes reorder status. |
| Import provenance | `provenance.import_batches`, `provenance.import_files`, `provenance.derived_inventory_rows` | Batch stores manifest/hash/path; per-table file stores SHA-256/count; derived rows document ledger-derived inventory pairs. |

Important database rules already present include exact `NUMERIC` monetary values, whole-unit integer quantities, foreign keys, row checks, unique keys, sales/PO arithmetic checks, effective-dated branch/warehouse mappings, distinct requester/approver constraints for selected documents, indexes on common relationship/status/date filters, and a composite employee/branch key used by orders.

### 6.3 Logical ER diagram

~~~mermaid
erDiagram
  BRANCHES {
    int branch_id PK
  }
  EMPLOYEES {
    int employee_id PK
  }
  DEPARTMENTS {
    int department_id PK
  }
  BRANCH_WAREHOUSE_SERVICES {
    int branch_id PK
    int warehouse_id PK
    date valid_from PK
  }
  WAREHOUSES {
    int warehouse_id PK
  }
  PRODUCT_CATEGORIES {
    int category_id PK
  }
  PRODUCTS {
    int product_id PK
  }
  SUPPLIERS {
    int supplier_id PK
  }
  SUPPLIER_PRODUCTS {
    int supplier_product_id PK
  }
  CUSTOMERS {
    int customer_id PK
  }
  SALES_ORDERS {
    int sales_order_id PK
  }
  SALES_ORDER_ITEMS {
    int sales_order_item_id PK
  }
  PAYMENTS {
    int payment_id PK
  }
  SHIPMENTS {
    int shipment_id PK
  }
  INVENTORY_RESERVATIONS {
    int reservation_id PK
  }
  INVENTORY_RESERVATION_ITEMS {
    int reservation_item_id PK
  }
  INVENTORY {
    int inventory_id PK
  }
  STOCK_MOVEMENTS {
    bigint movement_id PK
  }
  STOCK_TRANSFERS {
    int transfer_id PK
  }
  STOCK_TRANSFER_ITEMS {
    int transfer_item_id PK
  }
  PURCHASE_ORDERS {
    int purchase_order_id PK
  }
  PURCHASE_ORDER_ITEMS {
    int po_item_id PK
  }
  GOODS_RECEIPTS {
    int receipt_id PK
  }
  GOODS_RECEIPT_ITEMS {
    int receipt_item_id PK
  }
  RETURNS {
    int return_id PK
  }
  RETURN_ITEMS {
    int return_item_id PK
  }
  PAYMENT_REFUNDS {
    int refund_id PK
  }
  IMPORT_BATCHES {
    string batch_id PK
  }
  IMPORT_FILES {
    string batch_id PK
    string table_name PK
  }
  BRANCHES ||--o{ EMPLOYEES : employs
  DEPARTMENTS ||--o{ EMPLOYEES : groups
  EMPLOYEES ||--o{ EMPLOYEES : manages
  BRANCHES ||--o{ BRANCH_WAREHOUSE_SERVICES : maps
  WAREHOUSES ||--o{ BRANCH_WAREHOUSE_SERVICES : serves
  PRODUCT_CATEGORIES ||--o{ PRODUCTS : categorizes
  SUPPLIERS ||--o{ SUPPLIER_PRODUCTS : offers
  PRODUCTS ||--o{ SUPPLIER_PRODUCTS : sourced_as
  CUSTOMERS ||--o{ SALES_ORDERS : places
  BRANCHES ||--o{ SALES_ORDERS : owns
  SALES_ORDERS ||--|{ SALES_ORDER_ITEMS : contains
  PRODUCTS ||--o{ SALES_ORDER_ITEMS : sells
  WAREHOUSES ||--o{ SALES_ORDER_ITEMS : fulfills
  SALES_ORDERS ||--o{ PAYMENTS : paid_by
  SALES_ORDERS ||--o{ SHIPMENTS : ships
  SALES_ORDERS ||--o{ INVENTORY_RESERVATIONS : reserves
  INVENTORY_RESERVATIONS ||--|{ INVENTORY_RESERVATION_ITEMS : allocates
  INVENTORY ||--o{ INVENTORY_RESERVATION_ITEMS : reserves_pair
  WAREHOUSES ||--o{ INVENTORY : holds
  PRODUCTS ||--o{ INVENTORY : stocked_as
  WAREHOUSES ||--o{ STOCK_MOVEMENTS : records
  PRODUCTS ||--o{ STOCK_MOVEMENTS : moves
  STOCK_TRANSFERS ||--|{ STOCK_TRANSFER_ITEMS : contains
  SUPPLIERS ||--o{ PURCHASE_ORDERS : supplies
  PURCHASE_ORDERS ||--|{ PURCHASE_ORDER_ITEMS : contains
  PURCHASE_ORDERS ||--o{ GOODS_RECEIPTS : received_as
  GOODS_RECEIPTS ||--|{ GOODS_RECEIPT_ITEMS : contains
  RETURNS ||--|{ RETURN_ITEMS : contains
  SALES_ORDERS ||--o{ RETURNS : may_have
  RETURNS ||--o{ PAYMENT_REFUNDS : may_refund
  PAYMENTS ||--o{ PAYMENT_REFUNDS : refunds
  IMPORT_BATCHES ||--|{ IMPORT_FILES : proves
~~~

The frozen migration SQL is the authority for precise columns, constraints and indexes; this document is the architecture guide, not a replacement for DDL.

### 6.4 Proposed workflow persistence

Add an Alembic migration; do not edit revisions `0001` or `0002` after they have been applied. Suggested tables in `workflow`:

| Table | Important fields | Purpose / constraints |
|---|---|---|
| `workflow.requests` | `request_id UUID`, `request_type`, `current_revision`, `status`, `branch_id`, `warehouse_id`, `source_file_name`, `created_at` | Stable request identity and current lifecycle status. Branch/warehouse references point to `erp`; no human user FK. |
| `workflow.request_revisions` | `(request_id, revision)` PK, `payload JSONB`, `payload_sha256`, `created_at`, `source_file_sha256` | Immutable canonical business snapshot. Unique request/revision; hash covers normalized business fields, not approval cells. |
| `workflow.agent_runs` | `run_id UUID`, `request_id`, `revision`, `trigger_type`, `status`, timestamps, `summary_json` | Trace each initiated analysis. References request revision. |
| `workflow.agent_tasks` | `task_id UUID`, `run_id`, `parent_task_id`, `agent_name`, `task_type`, `status`, `attempt`, input/output JSONB, timestamps, error code | Persist all orchestrated handoffs; index `(run_id, created_at)` and `(status, created_at)`. Avoid retaining hidden chain-of-thought; store result, evidence refs and concise rationale only. |
| `workflow.approval_files` | `approval_file_id UUID`, request/revision, exported SHA-256, returned SHA-256, artifact paths, received/processed timestamps, status | Track generated and returned file artifacts. Preserve exact file bytes in an archive/object store; database stores hashes and safe relative references. |
| `workflow.approval_decisions` | `decision_id UUID`, `approval_file_id`, request/revision, decision, note, optional `manager_label`, recorded time | One accepted decision per request revision, enforced with a unique constraint. `manager_label` is descriptive only. |
| `workflow.approval_import_events` | event ID, approval file, event type, validation errors JSONB, timestamp | Append-only record of accepted and rejected import attempts. Do not lose a rejected/tampered-file trace when the business transaction rolls back. |
| `workflow.outbox` (optional) | event ID, type, payload, created/delivered times, retry count | Add only when reliable external notifications or async workers are introduced. |

Proposed operational constraints: request revision is immutable; one active decision per revision; `decision` limited to `approved`, `rejected`, `cancelled`, `needs_changes`; unique idempotency key for each import/transaction; task/run IDs are unique; foreign keys bind branch/warehouse/reference IDs; notes and JSON payloads are size-limited. Keep PII minimal and define retention before using real company data.

### 6.5 Multi-branch and multi-warehouse rules

- An operational task includes a branch and, when relevant, warehouse. Do not infer warehouse solely from an agent prompt.
- Resolve eligible warehouses through `branch_warehouse_services` for the transaction date. Enforce `can_fulfill` or `can_transfer` as appropriate.
- A central warehouse may serve multiple branches; availability is a warehouse/product fact, not a branch-wide unqualified number.
- Order employees must belong to the same branch as the order when an employee is attached.
- Inventory is keyed by warehouse/product. Available quantity is `quantity_on_hand - quantity_reserved`.
- For the initial sales MVP, reserve all requested lines or none. If not enough stock exists, request a revised order/warehouse choice; do not silently partially fulfill.
- On approval, re-check stock inside the write transaction and lock inventory rows in sorted `(warehouse_id, product_id)` order. If availability changed since review, make no order/reservation write and request a new review revision.
- Reservation changes do not create a stock movement; fulfillment/receipt/transfer/return/adjustment movements update the stock ledger and inventory projection together.

## 7. API design (proposed, not implemented)

Implement a versioned FastAPI application after workflow contracts are agreed. The file watcher/CLI and REST endpoint should call the same application services; API handlers must not contain domain SQL or business rules.

### 7.1 Private-network endpoints

| Method and path | Purpose | Idempotency / behavior |
|---|---|---|
| `POST /api/v1/requests/sales-orders` | Create request from validated JSON or an uploaded workbook; start Sales + Inventory analysis. | `Idempotency-Key`; returns request ID, revision, status and run ID. |
| `POST /api/v1/requests/purchase-proposals` | Analyze a replenishment request and create a draft review. | Same idempotency and scope rules. |
| `POST /api/v1/requests/returns` | Create a return-review request and collect order/payment facts. | Does not issue a refund. |
| `POST /api/v1/approvals/import` | Upload returned workbook for verification and processing. | File hash + request/revision deduplication; returns accepted/rejected/conflict outcome. |
| `GET /api/v1/requests/{request_id}` | Read request/revision state and current outcome. | No raw prompt/provider secrets. |
| `GET /api/v1/runs/{run_id}` | Read run summary and task statuses. | Show sanitized result/evidence references. |
| `GET /api/v1/reference/branches` | List supported branches. | Paginated/read-only. |
| `GET /api/v1/reference/branches/{branch_id}/warehouses?as_of=...` | Return warehouses mapped to a branch at a date. | Uses effective-dated service mapping. |
| `GET /api/v1/inventory` | Scoped stock snapshot by branch/warehouse/product. | Bounded filters and pagination. |
| `POST /api/v1/analysis/stock-risk` | Request an advisory analysis for a scope/date window. | Report/recommendation only; no inventory mutation. |

Agent task endpoints, if agents later become separate processes, belong on a private internal interface, not the manager-facing contract. Initially `dispatch_task()` is an in-process call.

### 7.2 Request and response conventions

- Use Pydantic request/response models, explicit enums, ISO date strings, decimal strings/JSON numbers handled as `Decimal`, and stable IDs.
- Every response carries a correlation/request ID; errors return a stable code, safe message, and field-level details where relevant.
- Expected error classes: `400` malformed file/input, `404` unknown business ID, `409` duplicate/stale revision or stock changed, `422` invalid business rule, `503` DB/model provider unavailable, `500` unexpected error with correlation ID.
- Do not return SQL, connection details, prompts, secrets, or raw tracebacks in an API response.
- No human login is defined in the MVP. Keep API and file storage on a private development network; do not expose unauthenticated write endpoints publicly. If a real company deploys this outside a trusted local environment, approval identity/access controls must be designed before that deployment.

## 8. End-to-end workflows and sequence diagrams

### 8.1 Sales request → analysis → approval workbook → order/reservation

1. Intake waits for a completed workbook, validates the template, identifiers and quantities, normalizes rows, assigns request/revision, and hashes canonical business fields.
2. Orchestrator verifies branch/warehouse mapping and calls Sales and Inventory tasks. Each agent uses read-only tools; it returns source-backed facts and recommendations.
3. Deterministic code calculates exact prices, discounts, tax and totals. The workbook contains the frozen order values, stock snapshot/time, request ID/revision/hash and editable decision/note cells.
4. Manager returns the workbook. Importer stores a receipt and returned-file hash, verifies business fields did not change, checks allowed decision and rejects stale/duplicate revisions.
5. If rejected/cancelled, record decision and leave business rows unchanged. If needs changes, return for a new revision. If approved, revalidate and lock the needed stock rows; atomically insert order, lines, reservation header/lines, update reserved quantities, decision and request status.
6. If stock is no longer available, write no order/reservation, record the conflict and issue a new review revision.

~~~mermaid
sequenceDiagram
  autonumber
  actor Operator
  participant Intake as File Intake
  participant Orch as Orchestrator
  participant Sales as Sales Agent
  participant Stock as Inventory Agent
  participant Tools as Read-only Tool Gateway
  participant DB as PostgreSQL
  participant Review as Workbook Service
  actor Manager
  participant Import as Approval Importer
  participant Tx as Transaction Service

  Operator->>Intake: Place completed sales request workbook
  Intake->>Intake: Parse, validate, assign request/revision, hash payload
  Intake->>DB: Persist request and immutable revision
  Intake->>Orch: Start analysis run
  Orch->>Sales: Typed sales task with branch scope
  Sales->>Tools: get_sales_summary / product facts
  Tools->>DB: Scoped SELECT
  DB-->>Tools: Facts + row references
  Tools-->>Sales: Tool result
  Sales-->>Orch: Structured facts/recommendation
  Orch->>Stock: Typed availability task
  Stock->>Tools: get_inventory_snapshot / branch warehouse map
  Tools->>DB: Scoped SELECT
  DB-->>Tools: Availability + reservation facts
  Tools-->>Stock: Tool result
  Stock-->>Orch: Structured facts/recommendation
  Orch->>Review: Facts + deterministic totals + request hash
  Review-->>Operator: Review workbook
  Operator->>Manager: Provide workbook for decision
  Manager-->>Import: Return decision and note fields
  Import->>DB: Store receipt/hash and validation event
  Import->>Import: Verify revision, payload hash and allowed fields
  alt Approved and stock still available
    Import->>Tx: execute_approved_order(request, revision)
    Tx->>DB: BEGIN; lock inventory; recheck; insert order/lines/reservations; audit
    DB-->>Tx: COMMIT
    Tx-->>Import: Committed order ID
  else Rejected, cancelled, needs changes, or conflict
    Import->>DB: Record outcome; no order/stock write
  end
~~~

### 8.2 Cross-agent coordination for a combined question

For “summarize sales and stock for this category,” the orchestrator can dispatch Sales and Inventory tasks in parallel. Each receives only its required branch/date/product scope. The orchestrator waits for both, merges their structured outputs, and labels partial results if one fails. Neither agent passes messages to the other or shares private reasoning.

~~~mermaid
sequenceDiagram
  autonumber
  participant Trigger
  participant O as Orchestrator
  participant S as Sales Agent
  participant I as Inventory Agent
  participant G as Read-only Tools
  participant DB as PostgreSQL
  participant R as Result Composer
  Trigger->>O: Sales/stock analysis request
  O->>O: Validate branch, date range and category scope
  par Sales facts
    O->>S: sales.summary task
    S->>G: get_sales_summary(scope)
    G->>DB: SELECT order/item aggregates
    DB-->>G: Facts + source refs
    G-->>S: Tool result
    S-->>O: SalesResult
  and Stock facts
    O->>I: inventory.availability task
    I->>G: get_inventory_snapshot(scope)
    G->>DB: SELECT stock/reservation pairs
    DB-->>G: Facts + source refs
    G-->>I: Tool result
    I-->>O: InventoryResult
  end
  O->>R: Both results + statuses
  R-->>Trigger: Combined report or explicit partial-result notice
~~~

### 8.3 Scheduled low-stock review

Scheduler triggers a scoped run (for example, all active mapped warehouses once daily). Inventory agent identifies below-reorder candidates from the view; Procurement agent compares supplier terms/open POs for those products; orchestrator creates an advisory/review workbook. No PO is issued until a valid manager approval is imported.

~~~mermaid
sequenceDiagram
  autonumber
  participant Scheduler
  participant O as Orchestrator
  participant I as Inventory Agent
  participant P as Procurement Agent
  participant G as Read-only Tools
  participant DB as PostgreSQL
  participant W as Workbook Service
  actor Manager
  Scheduler->>O: low_stock_scan(branch/warehouse scope, date)
  O->>I: Find low stock and available quantity
  I->>G: get_inventory_snapshot(scope)
  G->>DB: Scoped SELECT from inventory position / reservations
  DB-->>G: Candidate stock rows
  G-->>I: Tool result
  I-->>O: Candidate product IDs + evidence
  O->>P: Compare candidates with suppliers and open POs
  P->>G: get_supplier_options + get_open_purchase_orders
  G->>DB: Scoped SELECT
  DB-->>G: Lead time, supplier cost and PO facts
  G-->>P: Tool result
  P-->>O: Draft quantities/options; no PO write
  O->>W: Create review workbook with source snapshot hash
  W-->>Manager: Proposed replenishment review file
  Manager-->>W: Approval decision / note
  Note over W,DB: Approval importer and deterministic PO service execute only after validation.
~~~

### 8.4 Foundation data import (already implemented)

The current importer reads the manifest and CSVs, checks SHA-256/header/count/table inventory, obtains a transaction-scoped advisory lock, rejects a different dataset in a non-empty database, bulk-copies tables in dependency order, derives the documented 14 ledger-only stock snapshots, records provenance, advances identity sequences, runs 45 cross-table checks, and commits everything atomically. A failure rolls back the batch. Reimporting the same manifest verifies the existing database and creates no duplicates. Do not use the legacy generator's destructive database reset path for application workflows.

## 9. Deterministic business services

### 9.1 Sales order command

Proposed command: `execute_approved_order(request_id, revision, approval_event_id, idempotency_key)`.

1. Verify the request is in the approved-pending-execution state and its current revision/hash matches the approval.
2. Check branch, customer, products, price policy and effective warehouse service mapping.
3. Begin transaction; lock stock pair rows in stable sorted order (`SELECT ... FOR UPDATE`).
4. Recompute availability, exact decimals, discounts, tax and total in deterministic application/service code; do not use model-generated arithmetic.
5. Enforce all-or-nothing reservation for the MVP. If insufficient, rollback and record a conflict outcome; require a revised review.
6. Insert `sales_orders` and `sales_order_items`; insert reservation header/lines; increment `inventory.quantity_reserved`; insert accepted approval decision and workflow status/outbox event in the same business transaction.
7. Commit and return stable order/reservation IDs. Duplicate idempotency keys return the original result.

Fulfillment is a separate later operation: consume/release reservation, update inventory and append stock movement atomically when goods physically leave the warehouse. Approval alone does not reduce on-hand stock.

### 9.2 Purchase, transfer, and return commands

- **Purchase request:** agent may compare suppliers and recommend quantities. Approval command creates/updates a draft or approved PO according to a defined company policy; it does not contact a supplier or record an invoice payment. Receipt processing records goods receipt, increments stock and adds movement rows transactionally.
- **Warehouse transfer:** agent may suggest source/destination and quantity. Validate both warehouses against the branch service mapping and stock availability. Approved transfer dispatch records source movement; receipt records destination movement. Do not represent in-transit goods as available at either warehouse without an explicit in-transit model.
- **Return/refund:** agent may explain eligibility/status and draft a review. Manager approval and warehouse receipt/disposition are separate events. Refund execution requires a payment-provider integration, which does not exist now. Do not treat synthetic `payment_refunds` rows as a live processor integration.

## 10. Security, privacy, and trust boundaries

### 10.1 MVP decision and its limitation

The MVP intentionally has no application login or human user directory. The approval file is a workflow decision artifact, **not cryptographic identity proof**. A typed manager name is not authenticated. Anyone able to edit and return a valid approval file may be able to approve a transaction under the MVP assumptions. This limitation is acceptable for local synthetic-data development only. Before live production use involving real money, customer data, or inventory, add a verified manager identity/approval channel or equivalent signature plus authorization controls; do not expose approval import to an untrusted network.

### 10.2 Technical protections

- Keep the API, database port and approval folders on a private development network; do not publish the local Compose password or bind production PostgreSQL publicly.
- Use separate service credentials: read-only agent-tools DB role; write-limited transaction/import role; migration role used only during deployment. These are service roles, not human application accounts.
- Agents call allow-listed repository/tool methods only. No raw SQL, shell, filesystem, arbitrary URL/network, or payment tools are exposed to an LLM.
- Treat workbook cells, customer notes and source data as untrusted content, never as system instructions. Validate values and references independently of model output.
- Protect against spreadsheet formula injection by escaping untrusted text cells beginning with `=`, `+`, `-`, or `@`; do not execute macros; accept only the expected workbook schema and size.
- Approval verification compares immutable business columns to the canonical payload hash; file hashes are retained for the exact exported/returned artifacts. The returned file hash naturally differs after manager edits, so it must not be expected to equal the export hash.
- Parameterize SQL; limit query scope and output size; use transactions, unique idempotency keys, constraints and row locks for writes.
- Store model API keys only in deployment secrets/environment; never persist them in task input/output. Redact secrets and minimize customer PII in model context, logs and files.
- Keep audit events append-only. Log correlation IDs, action types, affected row IDs, file hashes, result status and timestamps; avoid passwords, full prompts, or sensitive notes in general logs.
- Use TLS, secret management, backups, encrypted storage and recovery procedures before production rollout.

## 11. Deployment and operations

### 11.1 Current verified foundation

The current database Compose file starts PostgreSQL 16 and an importer image. The database is exposed locally on port `55432`; the dataset mount is read-only. Current credentials are explicitly development-only. The foundation database volume persists when containers stop; `docker compose down -v` deletes it and must not be used casually.

~~~powershell
docker compose -f database/compose.yml up -d db
docker compose -f database/compose.yml build app
docker compose -f database/compose.yml run --rm app alembic -c database/alembic.ini upgrade head
docker compose -f database/compose.yml run --rm app python -m database.import_data /data
docker compose -f database/compose.yml run --rm app python -m pytest database/tests -v
~~~

Use Python 3.12 and PostgreSQL 16 for the target application. The root synthetic data generator still has its own environment/setup and its full database-loading command can reset its target `erp` schema. It is for dataset generation, not app writes.

### 11.2 Proposed application Compose topology

- `db`: PostgreSQL with named persistent volume and health check.
- `migrate`: one-shot Alembic upgrade job; run before API deployment.
- `api`: FastAPI process; private network; readiness check performs a lightweight DB query.
- `scheduler` or `worker`: optional process sharing the same code image; initially can invoke the orchestrator synchronously. Keep disabled until scheduled workflow is needed.
- `approval-files`: mounted inbox/review/archive volumes with file-size and retention limits. In production use a controlled object/file store.

The Compose topology must not publish unauthenticated write APIs publicly. For scale beyond one app instance, add a durable queue and outbox/inbox idempotency, not direct database triggers that call an LLM.

### 11.3 Operations

- **Health:** database ready, application ready, migrations at expected head.
- **Observability:** structured logs with run/task/correlation IDs; metrics for duration, tool calls, failures, retries, approval outcomes and conflicts; never use prompt/customer text as metric labels.
- **Backup:** scheduled PostgreSQL backups, retention, off-host encrypted copy, and periodic restore drills. Existing backup verification was a one-time local restore test, not a production backup schedule.
- **Data lifecycle:** archive approval artifacts by request ID/revision; define retention and deletion policies before real customer data.
- **Model provider:** isolate behind `LLMProvider` interface; configure timeouts, token/cost limits, output schema validation, retry limits and fallback to “needs review/no result”. No provider is currently selected or implemented.

## 12. Testing and acceptance criteria

### 12.1 Existing evidence

Phase 1: 11 integration tests passed on Python 3.12.16 and PostgreSQL 16; 45 reconciliations passed; backup restored and reconciled; all source hashes preserved. No API, approval-workbook round trip, agent, concurrency stress or model-evaluation test has been completed.

### 12.2 Required tests for proposed application

1. **Unit tests:** request parsing, canonicalization/hash, status transitions, decimal arithmetic, branch/warehouse date validity, task/result schemas, prompt-data separation.
2. **Repository tests:** tools are read-only, scoped correctly, parameterized, bounded; branch mapping and warehouse availability cases pass for all six branches and three warehouses.
3. **Approval-file tests:** valid approval; each allowed decision; changed business cells; stale revision; duplicate import; malformed/oversized file; formula-injection cell; missing/duplicate line; incorrect hash; invalid decision; note truncation.
4. **Transaction tests against PostgreSQL:** successful order/atomic reservation; insufficient stock race; concurrent approval of same request; duplicate idempotency key; rollback on any failed line; cancel/release; transfer and receipt balance; refund ceiling.
5. **Agent contract tests:** each agent returns valid typed result with source references; invalid output rejected; no data/timeout/provider error recorded; no direct write capability available in agent container/process.
6. **End-to-end scenarios:** order approved, rejected, needs changes then new revision; scheduled low-stock proposal with no PO before approval; return review with no automatic refund; one specialist failure with correct partial/error behavior.
7. **Security tests:** no secret in logs/task rows, no arbitrary SQL/URL/filesystem tool, spreadsheet formula escaping, API private-network configuration.
8. **Performance/concurrency:** realistic branch/warehouse workload, connection pool, lock contention and retry tests before any production sizing claim.

### 12.3 Definition of MVP complete

- A manager can submit/receive workbook files and produce a decision outcome without a login.
- A valid approval creates the intended record once; rejection/cancel/change request creates no operational write.
- Stale or modified files cannot silently authorize a different business payload.
- Multi-branch/warehouse restrictions are validated at request and transaction time.
- Agents use only read tools and return typed evidence-backed recommendations.
- Database constraints, service transactions, audit records, integration tests, backup/restore procedure and operator instructions are in place.
- User-facing documentation identifies the approval-identity limitation before any live deployment.

## 13. Suggested repository layout for Phase 2+

~~~text
app/
  api/                 # FastAPI routers and request/response schemas
  intake/              # workbook/JSON parsing, canonicalization, hashing
  orchestration/        # triggers, run lifecycle, task contracts, result merge
  agents/               # sales.py, inventory.py, procurement.py, returns.py
  tools/                # allow-listed read-only business queries
  repositories/         # SQLAlchemy persistence adapters
  services/             # deterministic sales, inventory, procurement, returns commands
  approvals/            # workbook rendering, import validation, state transitions
  audit/                # immutable workflow/provenance events
  settings.py
  main.py
database/
  migrations/           # existing foundation and additive workflow revisions
  models.py             # current reflected ERP mappings; add workflow mappings
  import_data.py        # corrected immutable snapshot importer
  integrity.py          # current reconciliation and future workflow checks
tests/
  unit/
  integration/
  acceptance/
docs/
  SOFTWARE_ARCHITECTURE_TECHNICAL_DESIGN.md
  AgenticERP_Agent_Architecture.drawio
  AgenticERP_MVP_Flowchart.drawio
~~~

Keep the data generator and its destructive reset workflow separate from `app/services`; an ERP request must never invoke the generator loader.

## 14. Recommended implementation sequence

1. Review this design and settle request workbook fields, status enums, all-or-none reservation rule, allowed manager-edited fields, and return/PO policies.
2. Add workflow/audit schema and migrations; add run/task/request revision repositories and tests.
3. Define Pydantic task/result contracts and a deterministic orchestrator with fake agents.
4. Build read-only database tools and scope tests; then implement one Sales + Inventory vertical slice.
5. Create request/review workbook export and approval importer, including hash/revision checks.
6. Implement approved sales command with row locks, idempotency and all-or-nothing reservation, then end-to-end tests.
7. Add Procurement and Returns agents and workflows, reusing the exact contract and approval boundary.
8. Add scheduled triggers, model provider adapter, evaluation suite, production identity controls, deployment/backup/monitoring.

This sequence gets the team a small, inspectable end-to-end system before parallel agent capability expands the surface area.

## 15. Reference artifacts

- Current corrected data and repair notes: `docs/DATA_REPAIR_REPORT.md`.
- Source entity definitions: `docs/DATA_DICTIONARY.md`.
- Phase 1 design and verification instructions: `database/README.md`, `database/VERIFICATION.md`.
- Agent runtime diagram: `docs/AgenticERP_Agent_Architecture.drawio`.
- MVP file approval and database flow: `docs/AgenticERP_MVP_Flowchart.drawio`.
- Authoritative schema: `database/migrations/versions/0001_schema.sql` plus additive migration `0002_derived_stock.py`.

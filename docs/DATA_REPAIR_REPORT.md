# Generated ERP Data Repair Report

**Completed:** 2026-10-28



**Seed:** `20240101`

**Corrected dataset:** `../out_repaired/`

## What changed

I fixed the generator and regenerated the dataset instead of hand-editing rows. The corrected set contains 31 CSV tables plus `data_manifest.json` and `validation_report.txt`. The manifest records table counts, seed, history window and SHA-256 for each CSV.

1. **Sales staff branch assignments:** order employees are now selected from the order branch, preferring Sales staff. The original 8,353/10,000 mismatch is now zero.
2. **Branch/warehouse coverage:** added `branch_warehouse_services` as an effective-dated many-to-many relationship. Each of six branches has exactly one primary warehouse; the central warehouse is explicitly available to branches whose primary is regional. Sales order lines use a warehouse mapped to the order branch.
3. **Department normalization:** added six department master rows and replaced the denormalized employee department field in the exported data with `employees.department_id` and a foreign key.
4. **Open-order reservations:** added `inventory_reservations` and `inventory_reservation_items`. An order remains `reserved` only when all its lines have stock allocated; otherwise it is `pending`. The 68 reserved orders have 238 linked reservation lines, and the inventory reserved projection reconciles to those lines.
5. **Refund traceability:** added 434 completed `payment_refunds` linked to returns and the original payments. Return line/header amounts use the same cent rounding as sales calculations and include the returned item tax. Refunds reconcile to returns and do not exceed their payments.
6. **Inventory valuation:** sale and restocked-return movement `unit_cost` now uses the product cost, not its retail selling price. The same cost basis is checked for opening stock and transfer movements.
7. **Operational actor consistency:** purchase requests use Procurement staff; approvals use Management/Finance and a different employee; receipt records use warehouse staff. Transfer requests/approvals use Warehouse/Management staff with separation of duties.
8. **Reproducible timestamps:** generated-at timestamps and branch, employee and customer dates are anchored to the configured history window rather than the machine's current date.
9. **Stock availability:** increased opening stock to reduce accidental system-wide stockouts while retaining shortage cases for agent scenarios. Unit fill improved from 77.4% to 89.5%; zero-stock pairs fell from 320 to 129; wholly unfulfilled orders fell from 388 to 125. This is still intentionally stressed synthetic data, not an asserted real-world service target.
10. **Validation and schema alignment:** added `validate_generated_data.py` to validate offline without connecting to PostgreSQL. It checks the generated headers against both export definitions and SQL columns, all declared CSV foreign keys and uniqueness rules, branch/warehouse and employee links, order arithmetic, PO receipts, payment/refund totals, reservations and inventory movement balances.

## Verification results

- `validate_generated_data.py --directory out_repaired`: **219 checks, 0 failures; DATASET VALID: YES**.
- Generated the same seed into a temporary second directory and compared SHA-256 hashes: **all 31 CSV hashes and the manifest matched**. The temporary directory was removed.
- PostgreSQL was not loaded or reset. `load_data.py` without `--generate-only` drops and recreates the `erp` schema, so I used the safe CSV-only path and validated those files offline.

## Files and preservation

- Corrected CSVs and validation output: `out_repaired/`.
- The original generated output became obsolete after regeneration. Cleanup removed 26 old files; Windows still holds `out/branches.csv` open, so that one stale file could not be deleted. The duplicate safety copy was removed during cleanup. Use `out_repaired/` for the corrected dataset; CSV generation now defaults there.
- Generator, SQL schema, data dictionary, README and validator changes are in the project. The corrected and backup data directories are ignored by Git so generated CSVs do not become source-code changes.

## Deliberately not fabricated

The data still has no general ledger, operating expenses, vendor invoice/payment history, payroll or compensation policy, or project/production records. I did not invent ledger postings or employee salaries from order and roster data; those require accounting recognition, cost-allocation, tax and compensation assumptions. The generated refunds and inventory costs repair traceability within the processes already modeled, but they do not make the dataset a complete accounting or payroll history.

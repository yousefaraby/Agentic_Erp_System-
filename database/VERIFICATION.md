# Phase 1 verification results

Verified on 2026-10-28 against PostgreSQL 16 in Docker and Python 3.12.16.

Completed and passed:
- Docker application image build and PostgreSQL health check.
- Alembic revisions 0001 and 0002 applied to an empty temporary database.
- Atomic import of all 31 source tables with SHA-256/count/header verification.
- Database reconciliation: 45 checks, zero failures.
- Repeated import retained exactly one provenance batch and created no duplicates.
- Final integration suite: 11 passed on Python 3.12.16. It checks ORM mappings, negative quantities, invalid references,
  cross-branch staff assignments, totals, drift detection, source tampering,
  identity sequence advancement and derived-stock provenance.
- Custom-format pg_dump backup restored into a separate disposable database;
  all 45 reconciliation checks passed after restore. Temporary verification
  databases were removed. The foundation database remains running on port 55432.

Source data finding: the existing CSV validator passed 219 checks but missed
14 movement-ledger stock pairs absent from inventory.csv (99 units). The first
live import correctly rolled back. Import now derives these inventory rows
from the movement ledger and records each in provenance.derived_inventory_rows.
The source CSVs and hashes were preserved. Imported inventory has 1,412 rows:
1,398 source rows plus 14 explicitly recorded derived rows.

Backup retained at database/backups/erp_foundation_3435f6ecea73.dump.

Not covered by this phase: load/performance certification, multi-process stress
or failover testing, production access control and point-in-time recovery.
No AI agents or backend business workflows are implemented here.

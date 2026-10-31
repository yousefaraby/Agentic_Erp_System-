"""Atomic immutable-snapshot import. Never overwrites an existing ERP dataset."""
import csv
import io
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from sqlalchemy import create_engine, text
from database.models import Base, prepare_models
from database.integrity import validate

def import_dataset(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "data_manifest.json").read_text())
    engine = create_engine(os.environ["ERP_DATABASE_URL"])
    prepare_models(engine)
    tables = list(Base.metadata.sorted_tables)
    payloads = {}
    for table in tables:
        name = table.name + ".csv"
        data = (directory / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != manifest["sha256"][name]: raise ValueError(f"Hash mismatch: {name}")
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        if rows[0] != [c.name for c in table.columns]: raise ValueError(f"Header mismatch: {name}")
        if len(rows)-1 != manifest["table_counts"][table.name]: raise ValueError(f"Count mismatch: {name}")
        payloads[table.name] = data
    if set(manifest["table_counts"]) != {t.name for t in tables}: raise ValueError("Table inventory mismatch")
    dataset_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7342001)"))
        if conn.execute(text("SELECT 1 FROM provenance.import_batches WHERE dataset_hash=:h"), {"h": dataset_hash}).scalar():
            validate(conn)
            print("Dataset already imported; integrity checks passed")
            return
        for table in tables:
            if conn.execute(text(f'SELECT EXISTS(SELECT 1 FROM erp."{table.name}")')).scalar():
                raise ValueError("Database is not empty; use a new database for a different snapshot")
        batch_id = str(uuid.uuid4())
        conn.execute(text("INSERT INTO provenance.import_batches(batch_id,dataset_hash,source_path,manifest) VALUES(CAST(:id AS uuid),:h,:p,CAST(:m AS jsonb))"), {"id":batch_id,"h":dataset_hash,"p":str(directory),"m":json.dumps(manifest)})
        with conn.connection.driver_connection.cursor() as cursor:
            for table in tables:
                with cursor.copy(f'COPY erp."{table.name}" FROM STDIN WITH (FORMAT CSV, HEADER TRUE)') as copy:
                    copy.write(payloads[table.name])
                conn.execute(text("INSERT INTO provenance.import_files VALUES(CAST(:id AS uuid),:t,:h,:n)"), {"id":batch_id,"t":table.name,"h":manifest["sha256"][table.name+".csv"],"n":manifest["table_counts"][table.name]})
        # Preserve the CSV snapshot while completing ledger-only stock pairs.
        conn.execute(text("""
          WITH missing AS (
            SELECT m.warehouse_id,m.product_id,sum(m.quantity_change) qty,max(m.movement_date) updated
            FROM erp.stock_movements m
            LEFT JOIN erp.inventory i USING(warehouse_id,product_id)
            WHERE i.inventory_id IS NULL GROUP BY 1,2
          ), inserted AS (
            INSERT INTO erp.inventory(inventory_id,warehouse_id,product_id,quantity_on_hand,quantity_reserved,reorder_point,last_counted_at,updated_at)
            SELECT (SELECT coalesce(max(inventory_id),0) FROM erp.inventory)+row_number() OVER(ORDER BY warehouse_id,product_id),
              m.warehouse_id,m.product_id,m.qty,0,p.reorder_point,
              (SELECT max(last_counted_at) FROM erp.inventory),m.updated
            FROM missing m JOIN erp.products p USING(product_id)
            RETURNING inventory_id
          )
          INSERT INTO provenance.derived_inventory_rows(inventory_id,batch_id,reason)
          SELECT inventory_id,CAST(:id AS uuid),'Missing source snapshot pair; quantity derived from complete stock movement ledger; count date inherited from synthetic snapshot' FROM inserted
        """), {"id":batch_id})
        # COPY preserves source IDs; advance identity sequences for subsequent inserts.
        for table in tables:
            for column in table.columns:
                if column.identity is not None:
                    conn.execute(text(
                        f'SELECT setval(pg_get_serial_sequence(:t,:c), '
                        f'COALESCE(max("{column.name}"),1), max("{column.name}") IS NOT NULL) '
                        f'FROM erp."{table.name}"'
                    ), {"t": "erp."+table.name, "c": column.name})
        validate(conn)
    print(f"Imported {len(tables)} tables atomically; batch={batch_id}")

if __name__ == "__main__": import_dataset(sys.argv[1] if len(sys.argv)>1 else "out_repaired")

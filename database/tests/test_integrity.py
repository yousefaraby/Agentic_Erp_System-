"""Integration tests require a migrated, imported dedicated database."""
import os
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from database.integrity import validate
from database.models import Base, prepare_models
from database.import_data import import_dataset

@pytest.fixture(scope="session")
def engine():
    url=os.environ.get("ERP_DATABASE_URL")
    if not url: pytest.skip("ERP_DATABASE_URL is not configured")
    return create_engine(url)

def test_all_models_and_reconciliation(engine):
    prepare_models(engine)
    assert len(Base.metadata.tables)==31
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM provenance.import_batches")).scalar_one()==1
        assert validate(c)["failures"]==0

def test_repeat_import_is_noop(engine):
    directory=os.environ.get("ERP_DATA_DIR", "/data")
    import_dataset(directory)
    import_dataset(directory)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM provenance.import_batches")).scalar_one()==1

@pytest.mark.parametrize("statement", [
    "UPDATE erp.inventory SET quantity_on_hand=-1 WHERE inventory_id=(SELECT min(inventory_id) FROM erp.inventory)",
    "UPDATE erp.sales_orders SET customer_id=-1 WHERE sales_order_id=(SELECT min(sales_order_id) FROM erp.sales_orders)",
    "UPDATE erp.sales_orders SET branch_id=CASE WHEN branch_id=1 THEN 2 ELSE 1 END WHERE sales_order_id=(SELECT min(sales_order_id) FROM erp.sales_orders)",
    "UPDATE erp.sales_orders SET branch_id=-1 WHERE sales_order_id=(SELECT min(sales_order_id) FROM erp.sales_orders)",
    "UPDATE erp.purchase_orders SET total_amount=total_amount+1 WHERE purchase_order_id=(SELECT min(purchase_order_id) FROM erp.purchase_orders)",
])
def test_constraints_reject_corruption(engine,statement):
    with engine.connect() as c:
        transaction=c.begin()
        try:
            with pytest.raises(IntegrityError): c.execute(text(statement))
        finally: transaction.rollback()

def test_cross_table_reconciliation_detects_drift(engine):
    with engine.connect() as c:
        transaction=c.begin()
        try:
            c.execute(text("UPDATE erp.inventory SET quantity_on_hand=quantity_on_hand+1 WHERE inventory_id=(SELECT min(inventory_id) FROM erp.inventory)"))
            with pytest.raises(ValueError, match="stock_ledger"): validate(c)
        finally: transaction.rollback()

def test_tampered_source_rejected_before_database_write(engine,tmp_path):
    import shutil
    from pathlib import Path
    source=Path(os.environ.get("ERP_DATA_DIR", "/data"))
    for path in source.glob("*.csv"): shutil.copy2(path,tmp_path/path.name)
    shutil.copy2(source/"data_manifest.json",tmp_path/"data_manifest.json")
    with (tmp_path/"branches.csv").open("ab") as f: f.write(b"\n")
    with pytest.raises(ValueError,match="Hash mismatch"):
        import_dataset(tmp_path)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM provenance.import_batches")).scalar_one()==1

def test_identity_after_import(engine):
    with engine.connect() as c:
        transaction=c.begin()
        try:
            highest=c.execute(text("SELECT max(department_id) FROM erp.departments")).scalar_one()
            new_id=c.execute(text("INSERT INTO erp.departments(department_code,name) VALUES('INTEGRITY_TEST','Integrity test department') RETURNING department_id")).scalar_one()
            assert new_id>highest
        finally: transaction.rollback()

def test_derived_inventory_has_source_provenance(engine):
    import csv
    from pathlib import Path
    source=Path(os.environ.get("ERP_DATA_DIR", "/data"))
    def read(name):
        with (source/(name+".csv")).open(newline="",encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    snapshot={(int(r["warehouse_id"]),int(r["product_id"])) for r in read("inventory")}
    ledger={(int(r["warehouse_id"]),int(r["product_id"])) for r in read("stock_movements")}
    with engine.connect() as c:
        derived=set(c.execute(text("SELECT i.warehouse_id,i.product_id FROM provenance.derived_inventory_rows d JOIN erp.inventory i USING(inventory_id) JOIN provenance.import_batches b USING(batch_id)")).all())
        assert derived == ledger-snapshot

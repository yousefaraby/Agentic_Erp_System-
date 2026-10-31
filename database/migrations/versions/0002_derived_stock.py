from alembic import op
revision="0002"
down_revision="0001"
branch_labels=None
depends_on=None

def upgrade():
    op.execute("""CREATE TABLE provenance.derived_inventory_rows (
      inventory_id integer PRIMARY KEY REFERENCES erp.inventory,
      batch_id uuid NOT NULL REFERENCES provenance.import_batches,
      reason text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    )""")

def downgrade():
    raise RuntimeError("Destructive downgrade disabled")

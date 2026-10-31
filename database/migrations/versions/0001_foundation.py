from pathlib import Path
from alembic import op
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    sql = Path(__file__).with_name("0001_schema.sql").read_text(encoding="utf-8")
    op.execute(sql)

def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a reviewed backup instead")

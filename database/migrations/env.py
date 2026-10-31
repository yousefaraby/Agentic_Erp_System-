import os
from alembic import context
from sqlalchemy import create_engine
url = os.environ["ERP_DATABASE_URL"]
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction(): context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction(): context.run_migrations()

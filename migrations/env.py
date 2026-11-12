from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base

config = context.config
connection = config.attributes.get("connection")

def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()

if connection is not None:
    migrate(connection)
else:
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        migrate(connection)

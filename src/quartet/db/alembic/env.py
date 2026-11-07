"""Alembic environment (online mode only).

The database URL comes from ``config.attributes["url"]``, else ``-x url=...``, else
``load_settings().migrator_database_url`` (``QUARTET_MIGRATOR_DATABASE_URL``); there is no
fallback to the application URL. It is never written to alembic.ini or logged.
"""

from alembic import context
from sqlalchemy import create_engine, pool

from quartet.config import load_settings
from quartet.db.alembic_url import resolve_migration_url
from quartet.db.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return resolve_migration_url(
        config.attributes.get("url"),
        context.get_x_argument(as_dictionary=True).get("url"),
        load_settings,
    )


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run against a database")
run_migrations_online()

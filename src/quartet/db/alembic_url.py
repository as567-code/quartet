"""Database URL resolution for Alembic (ruling R31a).

Order: ``config.attributes["url"]``, then ``-x url=...``, then
``load_settings().migrator_database_url``. There is deliberately no fallback to the application
URL: migrations must run as the schema-owning migrator role, never as ``quartet_app``.
"""

from collections.abc import Callable

from quartet.config import Settings


def resolve_migration_url(
    attributes_url: object, x_url: str | None, load: Callable[[], Settings]
) -> str:
    """Return the URL Alembic should connect with, or raise ``RuntimeError`` (no URL in it)."""
    for explicit in (attributes_url, x_url):
        if explicit:
            return str(explicit)
    migrator = load().migrator_database_url
    if migrator is None or not migrator.get_secret_value():
        raise RuntimeError(
            "no migration database URL: set QUARTET_MIGRATOR_DATABASE_URL (the quartet_migrator "
            "role); migrations never fall back to QUARTET_DATABASE_URL"
        )
    return migrator.get_secret_value()

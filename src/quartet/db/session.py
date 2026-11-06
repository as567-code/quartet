"""Engine and session helpers. Connection URLs are secrets and are never logged."""

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


def make_engine(url: SecretStr) -> Engine:
    """Create an engine. ``hide_parameters`` keeps bound values out of error messages."""
    return create_engine(url.get_secret_value(), pool_pre_ping=True, hide_parameters=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Yield a session; commit on success, roll back on any exception."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()

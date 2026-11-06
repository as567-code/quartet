"""ORM models (declarative base only for now; tables arrive with the first migration)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all Quartet tables."""

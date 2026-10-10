"""Database engine, session factory and the declarative base shared by all models."""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from krater.config import get_settings

# Explicit names for every constraint type, so Alembic autogenerate produces stable, predictable
# names instead of Postgres/SQLAlchemy defaults that can drift between runs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every Krater ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine, built once from settings and cached."""
    settings = get_settings()
    return create_engine(settings.database_url, future=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide session factory, bound to `get_engine()`."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a `Session`, closed after the request."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()

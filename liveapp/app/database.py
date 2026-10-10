"""Database engine, session factory and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_connect_args = {}
if settings.using_sqlite:
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.resolved_database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create every table declared on ``Base`` if it does not exist yet."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)

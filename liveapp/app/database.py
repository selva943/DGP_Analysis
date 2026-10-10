"""Database engine, session factory and schema bootstrap.

The engine is built lazily and every failure is captured rather than raised.
Vercel treats an exception during lifespan startup as fatal: the process exits
before the ASGI app is ever served, so every request becomes an anonymous
``500 FUNCTION_INVOCATION_FAILED`` with no usable log line. Keeping the failure
in :data:`engine_error` lets the app boot anyway and report the real problem
through ``/api/health``.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class DatabaseUnavailable(RuntimeError):
    """Raised on use when the engine could not be created."""


def _build_engine():
    """Create the engine, returning (engine, error) instead of raising."""
    connect_args = {"check_same_thread": False} if settings.using_sqlite else {}
    try:
        return (
            create_engine(
                settings.resolved_database_url,
                future=True,
                pool_pre_ping=True,
                connect_args=connect_args,
            ),
            None,
        )
    except Exception as exc:  # missing driver, malformed URL, bad options
        return None, f"{type(exc).__name__}: {exc}"


engine, engine_error = _build_engine()

_sessionmaker = (
    sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    if engine is not None
    else None
)


class _SessionFactory:
    """A callable that behaves like the old module-level ``SessionLocal``.

    Kept callable so existing ``SessionLocal()`` call sites keep working, but
    it reports *why* the database is unusable instead of ``TypeError: 'NoneType'
    object is not callable``.
    """

    def __call__(self, **kwargs) -> Session:
        if _sessionmaker is None:
            raise DatabaseUnavailable(
                f"Database engine could not be created ({engine_error}). "
                "Check DGP_DATABASE_URL and that a PostgreSQL driver is installed."
            )
        return _sessionmaker(**kwargs)


SessionLocal = _SessionFactory()


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    A missing or broken engine becomes a 503 carrying the reason, not an
    unhandled traceback, so the frontend can show something actionable.
    """
    try:
        db = SessionLocal()
    except DatabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        yield db
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {type(exc).__name__}. Check /api/health.",
        ) from exc
    finally:
        db.close()


def init_db() -> str | None:
    """Create every table that does not exist yet.

    Returns None on success or a human-readable error string on failure. It
    never raises, because a failure here must not take the whole deployment
    down; :func:`ping` and ``/api/health`` surface the problem instead.
    """
    from . import models  # noqa: F401  (register mappers)

    if engine is None:
        return engine_error or "engine unavailable"
    try:
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def ping() -> str | None:
    """One cheap round trip. Returns None when the database answers."""
    if engine is None:
        return engine_error or "engine unavailable"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None
"""Unauthenticated deployment diagnostics.

This exists because a serverless misconfiguration is otherwise invisible: the
function dies during import or lifespan startup and every request returns an
anonymous ``500 FUNCTION_INVOCATION_FAILED``. This route answers even when the
database is completely unreachable, so it can report *which* setting is wrong.

It deliberately exposes no connection strings, passwords or hostnames - only
booleans and error text. Keep it that way: it is unauthenticated by design so
it works before anyone can log in.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import database
from ..config import settings

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health():
    """Report configuration and connectivity without requiring a login."""
    checks: dict[str, object] = {}

    checks["database_url_set"] = bool(settings.database_url)
    checks["jwt_secret_strong"] = (
        bool(settings.jwt_secret)
        and "change-me" not in settings.jwt_secret
        and len(settings.jwt_secret) >= 32
    )
    checks["bootstrap_admin_configured"] = settings.bootstrap_admin is not None

    db_error = database.init_db() if database.engine is not None else database.engine_error
    checks["database_ok"] = db_error is None
    checks["database_error"] = db_error

    if db_error is None:
        checks["user_count"] = _safe_user_count()
        checks["has_dataset"] = _safe_has_dataset()

    checks["capabilities"] = {
        "import_enabled": settings.import_enabled,
        "local_processing_enabled": settings.local_processing_enabled,
    }

    # A degraded database is reported as 503 so monitoring can alert on it,
    # while still returning the full body explaining what is wrong.
    healthy = db_error is None
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", **checks},
    )


def _safe_user_count() -> int | None:
    try:
        from ..models import User

        db = database.SessionLocal()
        try:
            return db.query(User).count()
        finally:
            db.close()
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}"


def _safe_has_dataset() -> bool | None:
    try:
        from ..services import store

        db = database.SessionLocal()
        try:
            return store.active_version(db) is not None
        finally:
            db.close()
    except Exception:
        return None
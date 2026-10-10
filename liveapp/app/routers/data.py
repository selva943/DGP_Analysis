"""Dashboard data routes (read-only, any authenticated user)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import DatasetVersion, User
from ..services import activity, store

router = APIRouter(prefix="/api", tags=["data"])


def _require_active(db: Session) -> DatasetVersion:
    version = store.active_version(db)
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="No dataset has been published yet. An administrator must run 'Process data' first.",
        )
    return version


@router.get("/core")
def core(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Everything the dashboard needs to render: meta, tree, index, dates."""
    version = _require_active(db)
    data = store.active_core(db)
    if data is None:
        raise HTTPException(status_code=409, detail="Active dataset has no core payload.")
    data = dict(data)
    data["version"] = {
        "id": version.id,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "created_by": version.created_by,
    }
    return data


@router.get("/meta")
def meta(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    version = _require_active(db)
    data = store.active_core(db) or {}
    out = dict(data.get("meta", {}))
    out["version_id"] = version.id
    return out


@router.get("/tree")
def tree(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _require_active(db)
    data = store.active_core(db) or {}
    return data.get("tree", {})


@router.get("/dates")
def dates(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _require_active(db)
    data = store.active_core(db) or {}
    return data.get("dates", {})


@router.get("/opening-index")
def opening_index(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    _require_active(db)
    data = store.active_core(db) or {}
    return data.get("opening", {})


@router.get("/dse/{dse}/customers")
def dse_customers(
    dse: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    version = _require_active(db)
    return store.dse_customers(db, version.id, dse)


@router.post("/activity/open", status_code=204)
def record_open(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """Log one dashboard open for the Viewer Activity monitor."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")
    version = store.active_version(db)
    activity.record_open(
        db,
        username=user.username,
        role=user.role,
        display_name=user.display_name or user.username,
        ip=ip,
        dataset_version=version.id if version else None,
    )
    return Response(status_code=204)


@router.get("/customers/{dse}/{retailer}")
def customer(
    dse: str,
    retailer: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    version = _require_active(db)
    record = store.get_customer(db, version.id, dse, retailer)
    if record is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return record

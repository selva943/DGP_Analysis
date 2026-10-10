"""Persist and retrieve dashboard payloads (dataset versions)."""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import DashboardCore, DashboardCustomer, DatasetVersion


def save_payload(db: Session, payload: dict, created_by: str) -> int:
    """Store a new active dataset version and return its id."""
    db.execute(update(DatasetVersion).values(is_active=False))

    version = DatasetVersion(
        created_by=created_by,
        is_active=True,
        meta=payload.get("meta", {}),
    )
    db.add(version)
    db.flush()  # assign version.id

    core_data = {
        "meta": payload.get("meta", {}),
        "tree": payload.get("tree", {}),
        "dse_customers": payload.get("dse_customers", {}),
        "opening": payload.get("opening", {}),
        "dates": payload.get("dates", {}),
        "levels": payload.get("levels", []),
    }
    db.add(DashboardCore(version_id=version.id, data=core_data))

    customers: dict = payload.get("customers", {})
    rows = []
    for key, record in customers.items():
        dse, _, ret = key.partition("|")
        rows.append(
            {
                "version_id": version.id,
                "dse_code": dse,
                "retailer_code": ret,
                "record": record,
            }
        )
    if rows:
        db.bulk_insert_mappings(DashboardCustomer, rows)

    db.commit()
    return version.id


def active_version(db: Session) -> DatasetVersion | None:
    return (
        db.query(DatasetVersion)
        .filter(DatasetVersion.is_active.is_(True))
        .order_by(DatasetVersion.id.desc())
        .first()
    )


def active_core(db: Session) -> dict | None:
    version = active_version(db)
    if version is None or version.core is None:
        return None
    return version.core.data


def dse_customers(db: Session, version_id: int, dse: str) -> dict:
    rows = (
        db.query(DashboardCustomer)
        .filter(
            DashboardCustomer.version_id == version_id,
            DashboardCustomer.dse_code == dse,
        )
        .all()
    )
    return {f"{r.dse_code}|{r.retailer_code}": r.record for r in rows}


def get_customer(db: Session, version_id: int, dse: str, ret: str) -> dict | None:
    row = (
        db.query(DashboardCustomer)
        .filter(
            DashboardCustomer.version_id == version_id,
            DashboardCustomer.dse_code == dse,
            DashboardCustomer.retailer_code == ret,
        )
        .first()
    )
    return row.record if row else None

"""Build the dashboard payload from the normalized database tables.

Instead of re-implementing the tree/brand rollup, this reuses the proven
``build_dashboard.build()`` function and simply swaps its file-based loaders
for database-backed ones. The resulting payload is byte-for-byte the same
shape the static site used, so the frontend needs no rendering changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AchievedSbf,
    AgedReceivable,
    CustomerSnapshot,
    EasySbfGap,
    NameMaster,
    OpeningEasySbf,
    Retailer,
)

# Make the original batch scripts importable.
_PROJECT_ROOT = str(settings.project_root)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _df(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def _load_name_master(db: Session) -> pd.DataFrame:
    rows = db.query(NameMaster).all()
    return _df(
        [
            {
                "dse_code": r.dse_code,
                "dse_name": r.dse_name,
                "cluster": r.cluster,
                "asm": r.asm,
                "branch": r.branch,
                "channel": r.channel,
            }
            for r in rows
        ],
        ["dse_code", "dse_name", "cluster", "asm", "branch", "channel"],
    )


def _load_retailer_names(db: Session) -> dict[str, str]:
    return {
        r.retailer_code: r.retailer_name
        for r in db.query(Retailer).all()
        if r.retailer_name
    }


def _load_payment_modes(db: Session) -> dict[str, str]:
    return {
        r.retailer_code: r.payment_mode
        for r in db.query(Retailer).all()
        if r.payment_mode
    }


def _load_plan(db: Session) -> pd.DataFrame:
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "visit_date": r.visit_date.strftime("%Y-%m-%d"),
            "total_gap": int(r.total_gap or 0),
            "easy_sbf_gap": int(r.easy_sbf_gap or 0),
            "easy_sbf": list(r.easy_sbf or []),
        }
        for r in db.query(EasySbfGap).all()
    ]
    return _df(
        rows,
        ["dse_code", "retailer_code", "visit_date", "total_gap", "easy_sbf_gap", "easy_sbf"],
    )


def _load_achieved(db: Session) -> pd.DataFrame:
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "visit_date": r.visit_date.strftime("%Y-%m-%d"),
            "historical": int(r.historical_easy_sbf_gap or 0),
            "achieved": int(r.achieved_sbf_gap or 0),
            "still_open": int(r.still_open_easy_sbf_gap or 0),
            "new_gap": int(r.new_sbf_gap or 0),
            "achieved_sbf": list(r.achieved_sbf or []),
            "still_open_sbf": list(r.still_open_easy_sbf or []),
            "new_sbf": list(r.new_sbf or []),
            "comparison_status": r.comparison_status or "",
        }
        for r in db.query(AchievedSbf).all()
    ]
    return _df(
        rows,
        [
            "dse_code", "retailer_code", "visit_date", "historical", "achieved",
            "still_open", "new_gap", "achieved_sbf", "still_open_sbf", "new_sbf",
            "comparison_status",
        ],
    )


def _load_opening(db: Session) -> pd.DataFrame:
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "month": r.month,
            "total_gap": int(r.total_gap or 0),
            "easy_sbf_gap": int(r.easy_sbf_gap or 0),
            "easy_sbf": list(r.easy_sbf or []),
        }
        for r in db.query(OpeningEasySbf).all()
    ]
    return _df(
        rows,
        ["dse_code", "retailer_code", "month", "total_gap", "easy_sbf_gap", "easy_sbf"],
    )


def _load_ar(db: Session) -> pd.DataFrame:
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "balance_amount": float(r.balance_amount or 0),
            "net_amount": float(r.net_amount or 0),
            "aging_days": int(r.aging_days or 0),
            "ar_status": r.ar_status or "",
        }
        for r in db.query(AgedReceivable).all()
    ]
    return _df(
        rows,
        ["dse_code", "retailer_code", "balance_amount", "net_amount", "aging_days", "ar_status"],
    )


def _load_analytics(db: Session) -> pd.DataFrame:
    rows = [
        {
            "business_date": r.business_date.strftime("%Y-%m-%d"),
            "user_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "retailer_name": r.retailer_name,
            "gap_count": int(r.gap_count or 0),
            "easy_count": int(r.easy_count or 0),
            "hard_count": int(r.hard_count or 0),
            "with_sale_count": int(r.with_sale_count or 0),
            "easy_sold_count": int(r.easy_sold_count or 0),
            "planned_today": bool(r.planned_today),
            "suggested_today": bool(r.suggested_today),
        }
        for r in db.query(CustomerSnapshot).all()
    ]
    return _df(
        rows,
        [
            "business_date", "user_code", "retailer_code", "retailer_name",
            "gap_count", "easy_count", "hard_count", "with_sale_count",
            "easy_sold_count", "planned_today", "suggested_today",
        ],
    )


def build_payload(db: Session, log=print) -> dict:
    import build_dashboard as bd  # original batch builder

    bd.load_name_master = lambda: _load_name_master(db)
    bd.load_retailer_names = lambda: _load_retailer_names(db)
    bd.load_payment_modes = lambda: _load_payment_modes(db)
    bd.load_plan = lambda: _load_plan(db)
    bd.load_achieved = lambda: _load_achieved(db)
    bd.load_opening = lambda: _load_opening(db)
    bd.load_ar = lambda: _load_ar(db)
    bd.load_analytics = lambda: _load_analytics(db)

    log("Building dashboard payload from database ...")
    payload = bd.build()
    log(
        "  payload: "
        f"{payload['meta'].get('n_dse', 0)} DSE, "
        f"{payload['meta'].get('n_customers', 0)} customers"
    )
    return payload

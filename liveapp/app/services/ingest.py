"""Ingest source files into the normalized database tables.

This is the file -> database half of the pipeline. Every function performs a
full refresh of its target table(s) so a re-run always reflects the current
source files. The parsing rules match the original batch scripts exactly.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AchievedSbf,
    AgedReceivable,
    CustomerSnapshot,
    EasySbfGap,
    EasySbfMaster,
    NameMaster,
    OpeningEasySbf,
    Retailer,
)
from . import normalize as nz


def _read_csv_any_encoding(path: Path, **kwargs) -> pd.DataFrame:
    last: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False, **kwargs)
        except UnicodeDecodeError as exc:
            last = exc
            continue
    raise RuntimeError(f"Could not decode {path.name}: {last}")


def _reset(db: Session, model) -> None:
    db.query(model).delete()
    db.commit()


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def ingest_reference(db: Session, log=print) -> dict[str, int]:
    counts: dict[str, int] = {}

    # --- Name Master (hierarchy) ---
    nm_file = settings.updating_dir / "Name_master.xlsx"
    nm = pd.read_excel(nm_file)
    nm = nm.rename(columns=lambda c: str(c).strip())
    _reset(db, NameMaster)
    rows = []
    seen = set()
    for _, r in nm.iterrows():
        dse = nz.normalize_code(r.get("DSE_Code"))
        if not dse or dse in seen:
            continue
        seen.add(dse)
        rows.append(
            {
                "dse_code": dse,
                "dse_name": nz.clean_name(r.get("DSE_Name")),
                "cluster": nz.clean_name(r.get("ZSM")),
                "asm": nz.clean_name(r.get("ASM")),
                "branch": nz.clean_name(r.get("Branch")),
                "channel": nz.clean_name(r.get("BU")),
            }
        )
    if rows:
        db.bulk_insert_mappings(NameMaster, rows)
        db.commit()
    counts["name_master"] = len(rows)
    log(f"  name_master        : {len(rows)}")

    # --- Retailers: names from Calls Plan, payment mode from listing report ---
    pay_modes: dict[str, str] = {}
    pay_file = settings.updating_dir / "Retailer Listing Report.csv"
    if pay_file.is_file():
        try:
            d = pd.read_csv(pay_file, usecols=["retailer_code", "payment_mode"], dtype=str)
            d["retailer_code"] = d["retailer_code"].map(nz.normalize_code)
            d["payment_mode"] = d["payment_mode"].fillna("").map(nz.clean_name)
            for code, mode in zip(d["retailer_code"], d["payment_mode"]):
                if code and mode and code not in pay_modes:
                    pay_modes[code] = mode
        except Exception as exc:  # pragma: no cover - optional enrichment
            log(f"  payment modes skipped: {exc}")

    names: dict[str, str] = {}
    cp_file = settings.updating_dir / "Calls_plan.xlsx"
    cp = pd.read_excel(cp_file)
    cp = cp.rename(columns=lambda c: str(c).strip())
    for code, name in zip(cp["RetailerCode"], cp["RetailerName"]):
        c = nz.normalize_code(code)
        nm_name = nz.clean_name(name)
        if c and nm_name and c not in names:
            names[c] = nm_name

    all_codes = set(names) | set(pay_modes)
    _reset(db, Retailer)
    rrows = [
        {
            "retailer_code": c,
            "retailer_name": names.get(c, ""),
            "payment_mode": pay_modes.get(c, ""),
        }
        for c in all_codes
    ]
    if rrows:
        db.bulk_insert_mappings(Retailer, rrows)
        db.commit()
    counts["retailer"] = len(rrows)
    log(f"  retailer           : {len(rrows)}")

    # --- Easy SBF master ---
    es_file = settings.updating_dir / "Easy_SBF_List.xlsx"
    es = pd.read_excel(es_file)
    col = nz.find_column(list(es.columns), "easy_sbf")
    if col is None:
        raise RuntimeError("Easy_SBF_List.xlsx is missing the easy_sbf column")
    _reset(db, EasySbfMaster)
    erows = []
    seen_keys = set()
    for value in es[col]:
        key = nz.normalize_sbf_text(value)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        erows.append({"reason_key": key, "reason_text": nz.clean_sbf_readable(value)})
    if erows:
        db.bulk_insert_mappings(EasySbfMaster, erows)
        db.commit()
    counts["easy_sbf_master"] = len(erows)
    log(f"  easy_sbf_master    : {len(erows)}")

    return counts


# ---------------------------------------------------------------------------
# PLAN facts
# ---------------------------------------------------------------------------

def ingest_plan(db: Session, log=print) -> int:
    frames: list[pd.DataFrame] = []
    for f in sorted(settings.history_dir.glob("Easy_SBF_Gap_*.xlsx")):
        try:
            d = pd.read_excel(f, sheet_name="Easy_SBF_Gap")
        except Exception:
            continue
        if d.empty:
            continue
        d["dse_code"] = d["dse_code"].map(nz.normalize_code)
        d["retailer_code"] = d["retailer_code"].map(nz.normalize_code)
        d["visit_date"] = d["visit_date"].map(nz.parse_hist_date)
        d["total_gap"] = pd.to_numeric(d.get("total_gap"), errors="coerce").fillna(0).astype(int)
        d["easy_sbf_gap"] = pd.to_numeric(d.get("easy_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["easy_sbf"] = d["easy_sbf"].map(nz.split_sbf)
        frames.append(d)

    _reset(db, EasySbfGap)
    if not frames:
        log("  easy_sbf_gap       : 0 (no plan history)")
        return 0

    p = pd.concat(frames, ignore_index=True, sort=False)
    p = p[(p["dse_code"] != "") & (p["retailer_code"] != "") & p["visit_date"].notna()]
    # Same exclusions build_dashboard.py applies, so the ETL path and the
    # bundle import produce identical data.
    if settings.excluded_months:
        p = p[~p["visit_date"].str.startswith(tuple(settings.excluded_months))]
    if settings.excluded_dates:
        p = p[~p["visit_date"].isin(tuple(settings.excluded_dates))]
    p = p.drop_duplicates(
        subset=["dse_code", "retailer_code", "visit_date"], keep="first"
    )
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "visit_date": dt.date.fromisoformat(r.visit_date),
            "total_gap": int(r.total_gap),
            "easy_sbf_gap": int(r.easy_sbf_gap),
            "easy_sbf": list(r.easy_sbf),
        }
        for r in p.itertuples(index=False)
    ]
    if rows:
        db.bulk_insert_mappings(EasySbfGap, rows)
        db.commit()
    log(f"  easy_sbf_gap       : {len(rows)}")
    return len(rows)


# ---------------------------------------------------------------------------
# ACHIEVED facts
# ---------------------------------------------------------------------------

def _read_run_info(path: Path) -> dict:
    try:
        sheet = pd.read_excel(path, sheet_name="Run_Info")
    except Exception:
        return {}
    fc = nz.find_column(list(sheet.columns), "field") or (
        "Field" if "Field" in sheet.columns else None
    )
    vc = nz.find_column(list(sheet.columns), "value") or (
        "Value" if "Value" in sheet.columns else None
    )
    if fc is None or vc is None:
        return {}
    info: dict = {}
    for _, row in sheet.iterrows():
        key = str(row[fc]).strip()
        if not key or key.lower() in ("nan", "none"):
            continue
        info[key] = row[vc]
    return info


def ingest_achieved(db: Session, log=print) -> int:
    frames: dict = {}
    meta: list = []
    for f in sorted(settings.history_dir.glob("Achieved_SBF_*.xlsx")):
        info = _read_run_info(f)
        try:
            d = pd.read_excel(f, sheet_name="Achieved_SBF")
        except Exception:
            continue
        if d.empty:
            continue
        analysis = nz.parse_hist_date(info.get("Analysis_Visit_Date"))
        ts = str(info.get("Execution_Timestamp", ""))
        meta.append((analysis, ts, f))
        frames[f] = d

    date_of: dict = {}
    best: dict[str, tuple[Path, str]] = {}
    for analysis, ts, f in meta:
        if analysis is None:
            continue
        date_of[f] = analysis
        if analysis not in best or ts > best[analysis][1]:
            best[analysis] = (f, ts)
    chosen = [f for f, _ in best.values()]

    _reset(db, AchievedSbf)
    rows: list[dict] = []
    for f in chosen:
        d = frames[f]
        analysis = date_of.get(f)
        d["dse_code"] = d["dse_code"].map(nz.normalize_code)
        d["retailer_code"] = d["retailer_code"].map(nz.normalize_code)
        d["visit_date"] = d["visit_date"].map(nz.parse_hist_date)
        if d["visit_date"].isna().all():
            d["visit_date"] = analysis
        d = d[(d["dse_code"] != "") & (d["retailer_code"] != "") & d["visit_date"].notna()]
        for r in d.itertuples(index=False):
            rows.append(
                {
                    "dse_code": r.dse_code,
                    "retailer_code": r.retailer_code,
                    "visit_date": dt.date.fromisoformat(r.visit_date),
                    "historical_easy_sbf_gap": int(pd.to_numeric(getattr(r, "historical_easy_sbf_gap", 0), errors="coerce") or 0),
                    "achieved_sbf_gap": int(pd.to_numeric(getattr(r, "achieved_sbf_gap", 0), errors="coerce") or 0),
                    "achieved_sbf": nz.split_sbf(getattr(r, "achieved_sbf", "")),
                    "still_open_easy_sbf_gap": int(pd.to_numeric(getattr(r, "still_open_easy_sbf_gap", 0), errors="coerce") or 0),
                    "still_open_easy_sbf": nz.split_sbf(getattr(r, "still_open_easy_sbf", "")),
                    "new_sbf_gap": int(pd.to_numeric(getattr(r, "new_sbf_gap", 0), errors="coerce") or 0),
                    "new_sbf": nz.split_sbf(getattr(r, "new_sbf", "")),
                    "comparison_status": str(getattr(r, "comparison_status", "") or ""),
                }
            )
    if rows:
        db.bulk_insert_mappings(AchievedSbf, rows)
        db.commit()
    log(f"  achieved_sbf       : {len(rows)}")
    return len(rows)


# ---------------------------------------------------------------------------
# OPENING facts
# ---------------------------------------------------------------------------

def ingest_opening(db: Session, log=print) -> int:
    best: pd.DataFrame | None = None
    for f in sorted(settings.history_dir.glob("Opening_Easy_SBF_*.xlsx")):
        try:
            d = pd.read_excel(f, sheet_name="Opening_Easy_SBF")
        except Exception:
            continue
        if d.empty:
            continue
        best = d

    _reset(db, OpeningEasySbf)
    if best is None:
        log("  opening_easy_sbf   : 0 (no opening snapshot)")
        return 0

    d = best
    d["dse_code"] = d["dse_code"].map(nz.normalize_code)
    d["retailer_code"] = d["retailer_code"].map(nz.normalize_code)
    d["easy_sbf"] = d.get("easy_sbf", pd.Series(dtype=object)).map(nz.split_sbf)
    d = d[(d["dse_code"] != "") & (d["retailer_code"] != "")]
    rows = []
    for r in d.itertuples(index=False):
        rows.append(
            {
                "dse_code": r.dse_code,
                "retailer_code": r.retailer_code,
                "month": str(getattr(r, "month", "") or ""),
                "total_gap": int(pd.to_numeric(getattr(r, "total_gap", 0), errors="coerce") or 0),
                "easy_sbf_gap": int(pd.to_numeric(getattr(r, "easy_sbf_gap", 0), errors="coerce") or 0),
                "easy_sbf": list(r.easy_sbf),
            }
        )
    if rows:
        db.bulk_insert_mappings(OpeningEasySbf, rows)
        db.commit()
    log(f"  opening_easy_sbf   : {len(rows)}")
    return len(rows)


# ---------------------------------------------------------------------------
# AR + snapshots
# ---------------------------------------------------------------------------

def ingest_ar(db: Session, log=print) -> int:
    f = settings.input_dir / "AR.csv"
    _reset(db, AgedReceivable)
    if not f.is_file():
        log("  aged_receivable    : 0 (no AR.csv)")
        return 0
    d = _read_csv_any_encoding(f)
    d = d.rename(columns=lambda c: str(c).strip())
    d["dse_code"] = d["dse_code"].map(nz.normalize_code)
    d["retailer_code"] = d["retailer_code"].map(nz.normalize_code)
    d = d[(d["dse_code"] != "") & (d["retailer_code"] != "")]
    rows = [
        {
            "dse_code": r.dse_code,
            "retailer_code": r.retailer_code,
            "balance_amount": float(pd.to_numeric(getattr(r, "balance_amount", 0), errors="coerce") or 0),
            "net_amount": float(pd.to_numeric(getattr(r, "net_amount", 0), errors="coerce") or 0),
            "aging_days": int(pd.to_numeric(getattr(r, "aging_days", 0), errors="coerce") or 0),
            "ar_status": nz.clean_name(getattr(r, "ar_status", "")),
        }
        for r in d.itertuples(index=False)
    ]
    if rows:
        db.bulk_insert_mappings(AgedReceivable, rows)
        db.commit()
    log(f"  aged_receivable    : {len(rows)}")
    return len(rows)


def ingest_snapshots(db: Session, log=print) -> int:
    frames: list[pd.DataFrame] = []
    for parquet in sorted(settings.history_dir.glob("2026/*/analytics.parquet")):
        try:
            d = pd.read_parquet(parquet)
        except Exception:
            continue
        if d.empty:
            continue
        d["business_date"] = d["business_date"].astype(str)
        frames.append(d)

    _reset(db, CustomerSnapshot)
    if not frames:
        log("  customer_snapshot  : 0 (no snapshots)")
        return 0

    a = pd.concat(frames, ignore_index=True, sort=False)
    a["user_code"] = a["user_code"].map(nz.normalize_code)
    a["retailer_code"] = a["retailer_code"].map(nz.normalize_code)
    a = a[(a["user_code"] != "") & (a["retailer_code"] != "")]
    rows = []
    for r in a.itertuples(index=False):
        bd = nz.to_date(getattr(r, "business_date", None))
        if bd is None:
            continue
        rows.append(
            {
                "business_date": bd,
                "dse_code": r.user_code,
                "retailer_code": r.retailer_code,
                "retailer_name": nz.clean_name(getattr(r, "retailer_name", "")),
                "gap_count": int(getattr(r, "gap_count", 0) or 0),
                "easy_count": int(getattr(r, "easy_count", 0) or 0),
                "hard_count": int(getattr(r, "hard_count", 0) or 0),
                "with_sale_count": int(getattr(r, "with_sale_count", 0) or 0),
                "easy_sold_count": int(getattr(r, "easy_sold_count", 0) or 0),
                "planned_today": bool(getattr(r, "planned_today", False)),
                "suggested_today": bool(getattr(r, "suggested_today", False)),
            }
        )
    if rows:
        db.bulk_insert_mappings(CustomerSnapshot, rows)
        db.commit()
    log(f"  customer_snapshot  : {len(rows)}")
    return len(rows)


def ingest_all(db: Session, log=print) -> dict[str, int]:
    log("Ingesting reference data ...")
    counts = ingest_reference(db, log)
    log("Ingesting PLAN / ACHIEVED / OPENING ...")
    counts["easy_sbf_gap"] = ingest_plan(db, log)
    counts["achieved_sbf"] = ingest_achieved(db, log)
    counts["opening_easy_sbf"] = ingest_opening(db, log)
    log("Ingesting AR / snapshots ...")
    counts["aged_receivable"] = ingest_ar(db, log)
    counts["customer_snapshot"] = ingest_snapshots(db, log)
    return counts

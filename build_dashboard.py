"""build_dashboard.py

Build a self-contained HTML dashboard with the drill-down hierarchy:

    Cluster (ZSM/RSM) > ASM / TSM > Branch > DSE > Customer > Customer gap

Data sources (read-only):
- updating_files/Name_master.xlsx ......... hierarchy (ZSM/RSM, ASM, Branch, DSE)
- updating_files/Calls_plan.xlsx .......... retailer names
- history/Easy_SBF_Gap_*.xlsx ............. PLAN (per visit date)
- history/Achieved_SBF_*.xlsx ............. ACHIEVED (per analysis date)
- history/2026/08/*/analytics.parquet ..... daily snapshot enrichment

Output (deploy folder):
- output/site/index.html ................ the app shell (no data inside)
- output/site/dse-data/manifest.json .... file list + encoding (no secrets)
- output/site/dse-data/core.bin ......... tree / meta / dates / DSE customer index
- output/site/dse-data/c/<dse>.bin ...... one DSE's customer records (lazy-loaded)

Drag the whole output/site folder to your host, or zip it with zip_site.py and
import it in the live app.

Access control
--------------
The bundle is raw-deflate compressed JSON with no password and no encryption.
Access is controlled by the live app's login: an authenticated user loads the
core bundle, and each DSE's customer chunk is fetched on demand the first time
that DSE is viewed. Never host output/site on a public URL - serve the data
through the app, which checks the session on every request.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import zlib
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
UPDATING_DIR = BASE_DIR / "updating_files"
HISTORY_DIR = BASE_DIR / "history"
OUTPUT_DIR = BASE_DIR / "output"
INPUT_DIR = BASE_DIR / "Input"

HTML_TEMPLATE = BASE_DIR / "_dashboard_template.html"
SITE_DIR = OUTPUT_DIR / "site"
DATA_DIR_NAME = "dse-data"

# Visit-date prefixes to leave out of the dashboard, as YYYY-MM.
# 2026-08 is archived history; 2026-10-15 was pulled from the site on request
# (the workbook stays on disk, it is simply not rolled into the dashboard).
EXCLUDED_VISIT_MONTHS = ("2026-08",)
EXCLUDED_VISIT_DATES = ("2026-10-15",)


# ---------------------------------------------------------------------------
# Normalizers (mirror find_easy_sbf.py so joins line up)
# ---------------------------------------------------------------------------

def norm_code(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        text = str(int(value)) if value.is_integer() else repr(value)
    else:
        text = str(value)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text)
    text = text.replace("-", "_")
    text = re.sub(r"[^a-zA-Z0-9_]", "", text)
    text = text.strip("_").upper()
    return "" if text.upper() in {"", "NAN", "NONE", "NA", "N/A", "NULL", "<NA>", "NAT"} else text


def clean_name(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_hist_date(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def split_sbf(value: object) -> list[str]:
    """Split a comma-separated SBF string into clean, deduped items."""
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in str(value).split(","):
        item = clean_name(part)
        key = item.upper()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _col(d: pd.DataFrame, name: str) -> pd.Series:
    """Column values with a type-safe fallback for a missing column.

    Keeps Pylance happy by narrowing ``Series | None`` to ``Series`` while
    preserving the original runtime behaviour (a missing column becomes an
    empty series instead of crashing).
    """
    series = d.get(name)
    return series if series is not None else pd.Series(dtype=object)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_name_master() -> pd.DataFrame:
    df = pd.read_excel(UPDATING_DIR / "Name_master.xlsx")
    df = df.rename(columns=lambda c: str(c).strip())
    out = pd.DataFrame(
        {
            "dse_code": df["DSE_Code"].map(norm_code),
            "dse_name": df["DSE_Name"].map(clean_name),
            "cluster": df["ZSM"].map(clean_name),
            "asm": df["ASM"].map(clean_name),
            "branch": df["Branch"].map(clean_name),
            "channel": df["BU"].map(clean_name) if "BU" in df.columns else "",
        }
    )
    out = out[out["dse_code"] != ""]
    return out.drop_duplicates(subset=["dse_code"], keep="first")


def load_bu_mapping() -> dict[str, str]:
    """Unused after BU level removal; kept for channel fallback."""
    return {}


def load_retailer_names() -> dict[str, str]:
    cp = pd.read_excel(UPDATING_DIR / "Calls_plan.xlsx")
    cp = cp.rename(columns=lambda c: str(c).strip())
    best: dict[str, str] = {}
    for code, name in zip(cp["RetailerCode"], cp["RetailerName"]):
        c = norm_code(code)
        n = clean_name(name)
        if c and n and c not in best:
            best[c] = n
    return best


def load_payment_modes() -> dict[str, str]:
    """Load payment_mode from the Retailer Listing Report, keyed by retailer_code."""
    f = UPDATING_DIR / "Retailer Listing Report.csv"
    if not f.exists():
        return {}
    d = pd.read_csv(f, usecols=["retailer_code", "payment_mode"], dtype=str)
    d["retailer_code"] = d["retailer_code"].map(norm_code)
    d["payment_mode"] = d["payment_mode"].fillna("").map(clean_name)
    d = d[(d["retailer_code"] != "") & (d["payment_mode"] != "")]
    best: dict[str, str] = {}
    for code, mode in zip(d["retailer_code"], d["payment_mode"]):
        if code not in best:
            best[code] = mode
    return best


def load_ar() -> pd.DataFrame:
    """Load receivables (AR.csv) mapped by (dse_code, retailer_code).

    Columns used: aging_days, ar_status, balance_amount, net_amount.
    """
    f = INPUT_DIR / "AR.csv"
    if not f.exists():
        return pd.DataFrame()
    d = pd.read_csv(f, encoding="utf-8-sig", dtype=str)
    d = d.rename(columns=lambda c: str(c).strip())
    d["dse_code"] = d["dse_code"].map(norm_code)
    d["retailer_code"] = d["retailer_code"].map(norm_code)
    d = d[(d["dse_code"] != "") & (d["retailer_code"] != "")]
    d["balance_amount"] = pd.to_numeric(_col(d, "balance_amount"), errors="coerce").fillna(0)
    d["net_amount"] = pd.to_numeric(_col(d, "net_amount"), errors="coerce").fillna(0)
    d["aging_days"] = pd.to_numeric(_col(d, "aging_days"), errors="coerce").fillna(0).astype(int)
    d["ar_status"] = _col(d, "ar_status").fillna("").map(clean_name)
    return d


def load_analytics() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for parquet in sorted(HISTORY_DIR.glob("2026/*/analytics.parquet")):
        try:
            d = pd.read_parquet(parquet)
        except Exception:
            continue
        if d.empty:
            continue
        d["business_date"] = d["business_date"].astype(str)
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    a = pd.concat(frames, ignore_index=True, sort=False)
    a = a[~a["business_date"].str.startswith(("2026-08",))]
    a["user_code"] = a["user_code"].map(norm_code)
    a["retailer_code"] = a["retailer_code"].map(norm_code)
    keep = [
        "business_date", "user_code", "retailer_code", "retailer_name",
        "gap_count", "easy_count", "hard_count", "with_sale_count",
        "easy_sold_count", "planned_today", "suggested_today",
    ]
    for c in keep:
        if c not in a.columns:
            a[c] = None
    return a[keep]


def load_plan() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for f in sorted(HISTORY_DIR.glob("Easy_SBF_Gap_*.xlsx")):
        try:
            d = pd.read_excel(f, sheet_name="Easy_SBF_Gap")
        except Exception:
            continue
        if d.empty:
            continue
        d["dse_code"] = d["dse_code"].map(norm_code)
        d["retailer_code"] = d["retailer_code"].map(norm_code)
        d["visit_date"] = d["visit_date"].map(parse_hist_date)
        d["total_gap"] = pd.to_numeric(_col(d, "total_gap"), errors="coerce").fillna(0).astype(int)
        d["easy_sbf_gap"] = pd.to_numeric(_col(d, "easy_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["easy_sbf"] = _col(d, "easy_sbf").map(lambda v: split_sbf(v) if v is not None else [])
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    p = pd.concat(frames, ignore_index=True, sort=False)
    p = p[(p["dse_code"] != "") & (p["retailer_code"] != "") & p["visit_date"].notna()]
    p = p[~p["visit_date"].str.startswith(EXCLUDED_VISIT_MONTHS)]
    if EXCLUDED_VISIT_DATES:
        p = p[~p["visit_date"].isin(EXCLUDED_VISIT_DATES)]
    # A visit date processed more than once (e.g. find_easy_sbf.py run twice)
    # writes multiple history files with identical (dse, retailer, visit_date)
    # rows. Keep one copy per business key so the dashboard never double counts.
    p = p.drop_duplicates(
        subset=["dse_code", "retailer_code", "visit_date"], keep="first"
    )
    return p


def load_opening() -> pd.DataFrame:
    """Load the latest Opening_Easy_SBF snapshot.

    The Opening snapshot lists every customer found in the Input SBD reports
    with their Easy-SBF gaps for the running month. It is the locked
    "Total gap" baseline for the Easy-SBF summary-by-brand table. Only the
    most recent snapshot (last file in sorted order) is used.
    """
    best: pd.DataFrame | None = None
    for f in sorted(HISTORY_DIR.glob("Opening_Easy_SBF_*.xlsx")):
        try:
            d = pd.read_excel(f, sheet_name="Opening_Easy_SBF")
        except Exception:
            continue
        if d.empty:
            continue
        d["dse_code"] = d["dse_code"].map(norm_code)
        d["retailer_code"] = d["retailer_code"].map(norm_code)
        d["easy_sbf"] = _col(d, "easy_sbf").map(lambda v: split_sbf(v) if v is not None else [])
        best = d
    return best if best is not None else pd.DataFrame()


def load_achieved() -> pd.DataFrame:
    meta: list = []
    frames: dict = {}
    for f in sorted(HISTORY_DIR.glob("Achieved_SBF_*.xlsx")):
        try:
            info = pd.read_excel(f, sheet_name="Run_Info")
            info_map = dict(zip(info["Field"], info["Value"]))
        except Exception:
            continue
        try:
            d = pd.read_excel(f, sheet_name="Achieved_SBF")
        except Exception:
            continue
        if d.empty:
            continue
        analysis = info_map.get("Analysis_Visit_Date")
        analysis = parse_hist_date(analysis)
        ts = str(info_map.get("Execution_Timestamp", ""))
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

    frames_out: list[pd.DataFrame] = []
    for f in chosen:
        d = frames[f]
        analysis = date_of.get(f)
        d["dse_code"] = d["dse_code"].map(norm_code)
        d["retailer_code"] = d["retailer_code"].map(norm_code)
        d["visit_date"] = d["visit_date"].map(parse_hist_date)
        if d["visit_date"].isna().all():
            d["visit_date"] = analysis
        d["historical"] = pd.to_numeric(_col(d, "historical_easy_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["achieved"] = pd.to_numeric(_col(d, "achieved_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["still_open"] = pd.to_numeric(_col(d, "still_open_easy_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["new_gap"] = pd.to_numeric(_col(d, "new_sbf_gap"), errors="coerce").fillna(0).astype(int)
        d["achieved_sbf"] = _col(d, "achieved_sbf").map(lambda v: split_sbf(v) if v is not None else [])
        d["still_open_sbf"] = _col(d, "still_open_easy_sbf").map(lambda v: split_sbf(v) if v is not None else [])
        d["new_sbf"] = _col(d, "new_sbf").map(lambda v: split_sbf(v) if v is not None else [])
        d["comparison_status"] = _col(d, "comparison_status")
        frames_out.append(d)
    if not frames_out:
        return pd.DataFrame()
    a = pd.concat(frames_out, ignore_index=True, sort=False)
    a = a[(a["dse_code"] != "") & (a["retailer_code"] != "") & a["visit_date"].notna()]
    a = a[~a["visit_date"].str.startswith(("2026-08",))]
    return a


# ---------------------------------------------------------------------------
# Main build (pandas groupby based)
# ---------------------------------------------------------------------------

def pivot_by_date(df: pd.DataFrame, group_cols: list[str], metrics: dict[str, list[str]]) -> dict:
    """Return {group_tuple: {date: {metric: val}}}."""
    out: dict = {}
    for _, row in df.iterrows():
        key = tuple(row[c] for c in group_cols)
        node = out.setdefault(key, {})
        date = str(row["date_key"])
        slot = node.setdefault(date, {m: 0 for m in metrics})
        for m in metrics:
            cols = metrics[m]
            val = sum(int(row[c]) if row[c] is not None else 0 for c in cols)
            slot[m] += val
    return out


def build() -> dict:
    print("Loading Name Master ...")
    nm = load_name_master()
    print(f"  DSEs in master       : {len(nm)}")

    print("Loading retailer names from Calls Plan ...")
    retailer_names = load_retailer_names()
    print(f"  Retailer names loaded: {len(retailer_names)}")

    print("Loading payment modes from Retailer Listing Report ...")
    payment_modes = load_payment_modes()
    print(f"  Payment modes loaded : {len(payment_modes)}")

    print("Loading analytics snapshots ...")
    an = load_analytics()
    print(f"  Snapshot rows        : {len(an)}")

    print("Loading PLAN (Easy_SBF_Gap history) ...")
    plan = load_plan()
    print(f"  Plan rows            : {len(plan)}")

    print("Loading ACHIEVED (Achieved_SBF history) ...")
    ach = load_achieved()
    print(f"  Achieved rows        : {len(ach)}")

    print("Loading OPENING (Opening_Easy_SBF history) ...")
    opening = load_opening()
    print(f"  Opening rows         : {len(opening)}")

    print("Loading AR (aged receivables) ...")
    ar = load_ar()
    print(f"  AR rows              : {len(ar)}")

    # ---- Drop orphan DSEs (not present in the Name Master) ----
    # Hierarchy lookups later rely on every DSE code existing in `nm`.
    # History files written by older runs can contain DSE codes that are no
    # longer (or never were) in the Name Master. Those rows are invalid under
    # the "valid DSE" rule and must not crash the tree build.
    valid_dses = set(nm["dse_code"])
    def _drop_orphans(frame: pd.DataFrame, src: str) -> pd.DataFrame:
        if frame.empty:
            return frame
        before = len(frame)
        keep = frame[frame["dse_code"].isin(valid_dses)]
        dropped = before - len(keep)
        if dropped:
            print(f"  DROPPED {dropped} orphan-DSE row(s) from {src}")
        return keep
    plan = _drop_orphans(plan, "PLAN")
    ach = _drop_orphans(ach, "ACHIEVED")
    opening = _drop_orphans(opening, "OPENING")
    ar = _drop_orphans(ar, "AR")
    if not an.empty:
        before = len(an)
        an = an[an["user_code"].isin(valid_dses)]
        dropped = before - len(an)
        if dropped:
            print(f"  ANALYTICS dropped {dropped} orphan-DSE row(s)")

    opening_month = ""
    opening_brands: dict = {}
    opening_gap_map: dict = {}
    if not opening.empty:
        if "month" in opening.columns and opening["month"].notna().any():
            opening_month = str(opening["month"].iloc[0])
        for row in opening.to_dict("records"):
            key = (row["dse_code"], row["retailer_code"])
            opening_brands.setdefault(key, set()).update(row["easy_sbf"] or [])
            opening_gap_map[key] = int(row["easy_sbf_gap"] or 0)

    # ---- Enrich with hierarchy ----
    hier = nm.set_index("dse_code")
    plan["cluster"] = plan["dse_code"].map(hier["cluster"]).fillna("UNKNOWN")
    plan["asm"] = plan["dse_code"].map(hier["asm"]).fillna("UNKNOWN")
    plan["branch"] = plan["dse_code"].map(hier["branch"]).fillna("UNKNOWN")
    plan["dse_name"] = plan["dse_code"].map(hier["dse_name"]).fillna("")
    plan["channel"] = plan["dse_code"].map(hier["channel"]).fillna("")

    ach["cluster"] = ach["dse_code"].map(hier["cluster"]).fillna("UNKNOWN")
    ach["asm"] = ach["dse_code"].map(hier["asm"]).fillna("UNKNOWN")
    ach["branch"] = ach["dse_code"].map(hier["branch"]).fillna("UNKNOWN")

    # ---- Node metrics rollup ----
    # Build a nested tree of nodes each with metrics (all dates summed) and
    # per-date series.
    def empty_m():
        return {"dse": 0, "cust": 0, "planGap": 0, "planEasy": 0,
                "ach": 0, "open": 0, "new": 0, "hist": 0, "arBal": 0, "byDate": {}}

    def fmt_tree(level_node: dict) -> list:
        """Convert a nested dict tree into a list of {name, metrics, kids}."""
        rows = []
        for key in sorted(level_node):
            node = level_node[key]
            if isinstance(node, tuple):
                # leaf metrics tuple (metrics_dict, kids_list)
                metrics, kids = node
                rows.append({"name": key, "metrics": metrics, "kids": kids})
            else:
                rows.append({"name": key, "metrics": node[0], "kids": node[1]})
        return rows

    def merge(src: dict, dst: dict) -> None:
        for k in ("planGap", "planEasy", "ach", "open", "new", "hist", "cust", "arBal"):
            dst[k] += src[k]
        dst["dse"] += src["dse"]
        for date, slot in src["byDate"].items():
            tgt = dst["byDate"].setdefault(date, {"planGap": 0, "planEasy": 0, "ach": 0, "open": 0, "new": 0, "hist": 0})
            for k in slot:
                tgt[k] += slot[k]

    # tree stores: name -> asm -> branch -> dse -> metrics(cumulative) 
    # We also need customers per DSE and customer-level plan/achieved detail.
    customers: dict = {}  # (dse,retailer) -> record
    dse_metrics: dict = {}

    def m_add(m: dict, key: str, val: int, date: str) -> None:
        m[key] += val
        slot = m["byDate"].setdefault(
            date, {"planGap": 0, "planEasy": 0, "ach": 0, "open": 0, "new": 0, "hist": 0}
        )
        slot[key] += val

    # PLAN records
    dse_cust: dict = {}  # dse -> set(retailer)
    plan_cust: dict = {}  # (dse,retailer) -> set(dates)
    for row in plan.to_dict("records"):
        dse = row["dse_code"]; ret = row["retailer_code"]; date = row["visit_date"]
        dse_cust.setdefault(dse, set()).add(ret)
        plan_cust.setdefault((dse, ret), set()).add(date)
        m = dse_metrics.setdefault(dse, empty_m())
        m["dse"] = 1
        m_add(m, "planGap", int(row["total_gap"]), date)
        m_add(m, "planEasy", int(row["easy_sbf_gap"]), date)
        rec = customers.setdefault((dse, ret), {"plan": {}, "ach": {}})
        rec["plan"].setdefault(date, {"total": 0, "easy": 0, "sbf": []})
        slot = rec["plan"][date]
        slot["total"] += int(row["total_gap"])
        slot["easy"] += int(row["easy_sbf_gap"])
        sbf = row["easy_sbf"] or []
        for s in sbf:
            if s not in slot["sbf"]:
                slot["sbf"].append(s)

    # ACHIEVED records
    for row in ach.to_dict("records"):
        dse = row["dse_code"]; ret = row["retailer_code"]; date = row["visit_date"]
        m = dse_metrics.setdefault(dse, empty_m())
        m["dse"] = 1
        m_add(m, "ach", int(row["achieved"]), date)
        m_add(m, "open", int(row["still_open"]), date)
        m_add(m, "new", int(row["new_gap"]), date)
        m_add(m, "hist", int(row["historical"]), date)
        rec = customers.setdefault((dse, ret), {"plan": {}, "ach": {}})
        sl = rec["ach"].setdefault(date, {
            "historical": 0, "achieved": 0, "open": 0, "new": 0,
            "achieved_sbf": [], "open_sbf": [], "new_sbf": [], "status": ""})
        sl["historical"] += int(row["historical"])
        sl["achieved"] += int(row["achieved"])
        sl["open"] += int(row["still_open"])
        sl["new"] += int(row["new_gap"])
        for s in row["achieved_sbf"] or []:
            if s not in sl["achieved_sbf"]:
                sl["achieved_sbf"].append(s)
        for s in row["still_open_sbf"] or []:
            if s not in sl["open_sbf"]:
                sl["open_sbf"].append(s)
        for s in row["new_sbf"] or []:
            if s not in sl["new_sbf"]:
                sl["new_sbf"].append(s)
        sl["status"] = str(row["comparison_status"])

    for dse in dse_metrics:
        dse_metrics[dse]["cust"] = len(dse_cust.get(dse, set()))

    # ---- Brand (easy-SBF item) summary per DSE ----
    # A "brand" is one distinct easy-SBF item. Per DSE we count the number of
    # customers that planned / achieved / still have open each brand. A customer
    # counts once per brand even if the brand appears on several visit dates.
    # These per-DSE tables are rolled up through the hierarchy along with the
    # numeric metrics so every node (Cluster/ASM/Branch/DSE) can show its own
    # brand summary.
    #
    # Locked "Total gap": the Total gap column counts every customer in the
    # OPENING Easy SBF snapshot (from the Input SBD reports) whose easy-SBF
    # list contains the brand. Later daily runs do NOT move the Total gap.
    # Achieved / Balance keep accumulating from the whole achieved history.
    # When no Opening file exists yet it falls back to the oldest plan date.
    # This applies to the Easy-SBF summary-by-brand table only.
    baseline: dict = {}
    if opening_brands:
        baseline = opening_brands
    else:
        oldest_plan_date = min(plan["visit_date"]) if not plan.empty else None
        for (dse, ret), rec in customers.items():
            if oldest_plan_date is not None and oldest_plan_date in rec["plan"]:
                baseline[(dse, ret)] = set(rec["plan"][oldest_plan_date]["sbf"])

    all_brands: set = set()
    dse_brands: dict = {}
    for (dse, ret), brands in baseline.items():
        db = dse_brands.setdefault(dse, {})
        for b in brands:
            db.setdefault(b, {"planned": 0, "achieved": 0, "gap": 0})["planned"] += 1
        all_brands.update(brands)

    for (dse, ret), rec in customers.items():
        achieved = set()
        gap = set()
        for slot in rec["ach"].values():
            achieved.update(slot["achieved_sbf"])
            gap.update(slot["open_sbf"])
        if not (achieved or gap):
            continue
        db = dse_brands.setdefault(dse, {})
        for b in achieved:
            db.setdefault(b, {"planned": 0, "achieved": 0, "gap": 0})["achieved"] += 1
        for b in gap:
            db.setdefault(b, {"planned": 0, "achieved": 0, "gap": 0})["gap"] += 1
        all_brands.update(achieved)
        all_brands.update(gap)

    # Some DSEs only appear in the Opening snapshot (no plan / achieved records
    # of their own). They are added to the metrics map so their brand counts
    # roll up through the tree, but flagged as opening-only so the JS children
    # table can hide them (their numeric metrics are all zero).
    existing_dses = set(dse_metrics.keys())
    for dse in dse_brands:
        m = dse_metrics.setdefault(dse, empty_m())
        m["brands"] = dse_brands[dse]
    opening_only_dses = set(dse_brands) - existing_dses

    # Per-DSE opening brand counts for OPENING-ONLY customers (customers not in
    # the plan/achieved data). Used by the JS filtered brand table to keep the
    # locked Total gap baseline without adding zero-stat customers to the
    # drill-down tables.
    opening_by_dse: dict = {}
    for (dse, ret), brands in opening_brands.items():
        if (dse, ret) in customers:
            continue
        ob = opening_by_dse.setdefault(dse, {})
        for b in brands:
            ob[b] = ob.get(b, 0) + 1

    # Aggregate AR by (dse,retailer): balance, net, max aging, doc count, per-status balance
    ar_records: dict = {}
    for row in ar.to_dict("records"):
        key = (row["dse_code"], row["retailer_code"])
        rec = ar_records.setdefault(
            key, {"balance": 0.0, "net": 0.0, "aging": 0, "docs": 0, "status": {}}
        )
        rec["balance"] += float(row["balance_amount"])
        rec["net"] += float(row["net_amount"])
        rec["docs"] += 1
        if row["aging_days"] > rec["aging"]:
            rec["aging"] = row["aging_days"]
        s = row["ar_status"] or "(none)"
        rec["status"][s] = rec["status"].get(s, 0.0) + float(row["balance_amount"])

    # Seed AR balance into DSE leaf metrics BEFORE tree rollup so it rolls up
    for (dse, ret) in customers:
        ar_rec = ar_records.get((dse, ret))
        if ar_rec:
            dse_metrics[dse]["arBal"] += round(ar_rec["balance"])

    # ---- Build nested hierarchy tree ----
    # node structure: cluster -> asm -> branch -> dse -> metrics
    tree = {}
    for dse, m in dse_metrics.items():
        if dse not in hier.index:
            print(f"  WARNING: skipping DSE {dse} missing from Name Master")
            continue
        h = hier.loc[dse]
        cluster = h["cluster"] or "UNKNOWN"
        asm = h["asm"] or "UNKNOWN"
        br = h["branch"] or "UNKNOWN"
        node = (
            tree.setdefault(cluster, {})
            .setdefault(asm, {})
            .setdefault(br, {})
            .setdefault(dse, m)
        )

    def merge_brands(src: dict, dst: dict) -> None:
        for b, s in src.items():
            d = dst.setdefault(b, {"planned": 0, "achieved": 0, "gap": 0})
            d["planned"] += s["planned"]
            d["achieved"] += s["achieved"]
            d["gap"] += s["gap"]

    def roll(dct: dict) -> tuple:
        """Each value is either a metrics dict (leaf) or {child_key: value}.

        Returns (metrics, brands, children, opening_only). A node is flagged
        opening-only when it (or every descendant) exists only in the Opening
        snapshot with no plan / achieved data; such nodes are hidden from the
        children table but still contribute their brand counts.
        """
        children = []
        metrics = empty_m()
        brands = {}
        all_opening_only = True
        for key, val in dct.items():
            if isinstance(val, dict) and "byDate" in val:
                # leaf metrics dict
                metrics_leaf = val
                leaf_brands = metrics_leaf.get("brands", {})
                is_oo = key in opening_only_dses
                children.append({
                    "name": key,
                    "dname": (hier["dse_name"].get(key, "") or ""),
                    "metrics": metrics_leaf,
                    "brands": leaf_brands,
                    "kids": [],
                    "openingOnly": is_oo,
                })
                merge(metrics_leaf, metrics)
                merge_brands(leaf_brands, brands)
                if not is_oo:
                    all_opening_only = False
            else:
                sub_m, sub_b, sub_children, sub_oo = roll(val)
                children.append({
                    "name": key, "metrics": sub_m, "brands": sub_b,
                    "kids": sub_children, "openingOnly": sub_oo,
                })
                merge(sub_m, metrics)
                merge_brands(sub_b, brands)
                if not sub_oo:
                    all_opening_only = False
        return metrics, brands, children, all_opening_only

    top_children = []
    grand = empty_m()
    grand_brands: dict = {}
    for cluster in sorted(tree):
        cl_m, cl_b, cl_kids, _ = roll(tree[cluster])
        top_children.append({"name": cluster, "metrics": cl_m, "brands": cl_b, "kids": cl_kids})
        merge(cl_m, grand)
        merge_brands(cl_b, grand_brands)

    # ---- Customers: build per-DSE arrays and flat map ----
    customer_payload: dict = {}  # (dse|retailer) -> record with names etc.
    dse_level: dict = {}  # dse -> list of customers
    retail_final = {}

    for (dse, ret), rec in customers.items():
        h = hier.loc[dse] if dse in hier.index else None
        rname = retailer_names.get(ret, "")
        record = {
            "dse": dse,
            "dse_name": (h["dse_name"] if h is not None else ""),
            "ret": ret,
            "name": rname,
            "channel": (h["channel"] if h is not None else ""),
            "plan_date_count": len(rec["plan"]),
            "plan": {},
            "ach": {},
        }
        # sort plan dates
        for date in sorted(rec["plan"]):
            record["plan"][date] = rec["plan"][date]
        for date in sorted(rec["ach"]):
            record["ach"][date] = rec["ach"][date]
        # summary
        total_easy = sum(v["easy"] for v in rec["plan"].values())
        total_gap = sum(v["total"] for v in rec["plan"].values())
        achieved = sum(v["achieved"] for v in rec["ach"].values())
        open_ = sum(v["open"] for v in rec["ach"].values())
        new_ = sum(v["new"] for v in rec["ach"].values())
        hist = sum(v["historical"] for v in rec["ach"].values())
        record["total_easy"] = total_easy
        record["total_gap"] = total_gap
        record["achieved"] = achieved
        record["open"] = open_
        record["new"] = new_
        record["hist"] = hist
        # AR summary for this customer (balance, net, max aging, docs, per-status balance)
        ar_rec = ar_records.get((dse, ret))
        if ar_rec:
            statuses = sorted(ar_rec["status"].items(), key=lambda kv: -kv[1])
            record["ar"] = {
                "balance": round(ar_rec["balance"]),
                "net": round(ar_rec["net"]),
                "aging": int(ar_rec["aging"]),
                "docs": int(ar_rec["docs"]),
                "status": [{"s": s, "bal": round(b)} for s, b in statuses],
            }
        else:
            record["ar"] = None
        if rec["plan"]:
            latest_plan_date = sorted(rec["plan"])[-1]
            record["latest_sbf"] = list(rec["plan"][latest_plan_date]["sbf"])
            record["latest_easy"] = rec["plan"][latest_plan_date]["easy"]
            record["latest_date"] = latest_plan_date
        else:
            record["latest_sbf"] = []
            record["latest_easy"] = 0
            record["latest_date"] = ""
        record["opening"] = sorted(opening_brands.get((dse, ret), set()))
        record["opening_gap"] = opening_gap_map.get((dse, ret), 0)
        record["payment_mode"] = payment_modes.get(ret, "")
        retail_final[(dse, ret)] = record

    # build per-DSE customer arrays sorted by latest_easy desc (store keys only;
    # the full record lives once in the flat `customers` map)
    dse_customers = {}
    ordered = sorted(retail_final.items(), key=lambda kv: (-kv[1]["latest_easy"], kv[0]))
    for (dse, ret), _record in ordered:
        dse_customers.setdefault(dse, []).append(ret)

    # Attach customers + metrics to dse leaf nodes (already have metrics from roll)
    for dse, cust_list in dse_customers.items():
        m = dse_metrics.get(dse)
        # annotate leaf metrics with customerCount - already in cust
        pass

    # Analytics enrichment: attach snapshots to retailer records by dse+ret
    # (user_code in analytics == dse_code)
    snapshots = {}  # (dse,ret) -> list of {date, with_sale, easy_sold, gap, easy, planned, suggested}
    for _, row in an.iterrows():
        dse = row["user_code"]; ret = row["retailer_code"]
        key = (dse, ret)
        if key not in snapshots:
            snapshots[key] = []
        snapshots[key].append({
            "date": str(row["business_date"]),
            "with_sale": int(row["with_sale_count"]) if row["with_sale_count"] is not None else 0,
            "easy_sold": int(row["easy_sold_count"]) if row["easy_sold_count"] is not None else 0,
            "gap": int(row["gap_count"]) if row["gap_count"] is not None else 0,
            "easy": int(row["easy_count"]) if row["easy_count"] is not None else 0,
            "planned": bool(row["planned_today"]),
            "suggested": bool(row["suggested_today"]),
        })
    for (dse, ret), record in retail_final.items():
        record["snap"] = snapshots.get((dse, ret), [])

    # customer flat lookup
    customer_map = {}
    for key, record in retail_final.items():
        customer_map[f"{key[0]}|{key[1]}"] = record

    payload = {
        "meta": {
            "generated": dt.datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
            "plan_files": len(list(HISTORY_DIR.glob("Easy_SBF_Gap_*.xlsx"))),
            "achieved_files": len(list(HISTORY_DIR.glob("Achieved_SBF_*.xlsx"))),
            "snapshot_days": sorted({str(x) for x in an["business_date"].unique()}) if not an.empty else [],
            "plan_dates": sorted({x for x in plan["visit_date"].dropna().unique()}) if not plan.empty else [],
            "achieved_dates": sorted({x for x in ach["visit_date"].dropna().unique()}) if not ach.empty else [],
            "n_customers": len(retail_final),
            "n_dse": len(dse_metrics),
            "all_brands": sorted(all_brands),
            "channels": sorted({str(x).strip() for x in nm["channel"].dropna() if str(x).strip()}) if "channel" in nm.columns else [],
            "opening_month": opening_month,
            "opening_customers": len(opening_brands),
            "opening_only_dses": sorted(opening_only_dses),
        },
        "tree": {"name": "All", "metrics": grand, "brands": grand_brands, "kids": top_children},
        "dse_customers": dse_customers,
        "opening": opening_by_dse,
        "customers": customer_map,
        "dates": {
            "plan": sorted({x for x in plan["visit_date"].dropna().unique()}) if not plan.empty else [],
            "achieved": sorted({x for x in ach["visit_date"].dropna().unique()}) if not ach.empty else [],
            "snap": sorted({str(x) for x in an["business_date"].unique()}) if not an.empty else [],
        },
        "levels": ["cluster", "asm", "branch", "dse"],
    }
    return payload


# ---------------------------------------------------------------------------
# Password protection
# ---------------------------------------------------------------------------

def _deflate_raw(data: bytes) -> bytes:
    """Raw-deflate (no zlib/gzip header) so the browser can inflate it with
    DecompressionStream('deflate-raw')."""
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    out = co.compress(data)
    out += co.flush()
    return out


def _safe_chunk_name(dse: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", dse):
        raise SystemExit(
            f"ERROR: DSE code {dse!r} contains characters that are unsafe for "
            "chunk filenames. Rename the code in Name_master.xlsx first."
        )
    return dse


def write_split_site(payload: dict, template: str) -> None:
    """Split the payload into a small core bundle plus one file per DSE, and
    write the deployable static site under SITE_DIR.

    Files are raw-deflate compressed JSON. There is no password and no
    encryption: the live app authenticates users before serving any data, so
    the bundle is a transport format, not a secret.
    """
    customers: dict = payload["customers"]
    chunks: dict[str, dict] = {}
    for key, rec in customers.items():
        dse = key.split("|", 1)[0]
        chunks.setdefault(dse, {})[key] = rec

    core = {
        "meta": payload["meta"],
        "tree": payload["tree"],
        "dse_customers": payload["dse_customers"],
        "opening": payload["opening"],
        "dates": payload["dates"],
        "levels": payload["levels"],
    }

    if "/*__SECURE__*/" in template:
        raise SystemExit("ERROR: template still contains the old /*__SECURE__*/ marker; it is not the split-bundle loader.")
    if DATA_DIR_NAME + "/manifest.json" not in template:
        raise SystemExit("ERROR: template does not reference dse-data/manifest.json; wrong template version.")

    data_dir = SITE_DIR / DATA_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}

    def emit(rel: str, obj) -> tuple[int, int]:
        plaintext = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = _deflate_raw(plaintext)
        target = data_dir / (rel + ".bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        files[rel] = {"encoding": "deflate-raw", "bytes": len(blob)}
        return len(plaintext), len(blob)

    core_sizes = emit("core", core)
    chunk_stats = []
    for dse in sorted(chunks):
        rel = "c/" + _safe_chunk_name(dse)
        chunk_stats.append((emit(rel, chunks[dse]), dse))

    manifest = {
        "v": 3,
        "encoding": "deflate-raw",
        "encrypted": False,
        "files": files,
    }
    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    index_path = SITE_DIR / "index.html"
    index_path.write_text(template, encoding="utf-8")

    total_plain = core_sizes[0] + sum(s[0][0] for s in chunk_stats)
    total_wire = core_sizes[1] + sum(s[0][1] for s in chunk_stats)
    print(f"Wrote {index_path}")
    print(f"  core.bin : json {core_sizes[0]/1e6:6.2f} MB -> wire {core_sizes[1]/1e6:6.2f} MB")
    if chunk_stats:
        (sizes, dse) = max(chunk_stats, key=lambda s: s[0][1])
        print(f"  chunks   : {len(chunk_stats)} files, largest {dse} wire {sizes[1]/1e6:.2f} MB")
    print(f"  TOTAL    : plain JSON {total_plain/1e6:.1f} MB -> transferred {total_wire/1e6:.1f} MB")
    print("  No password: access is controlled by the app's login.")


if __name__ == "__main__":
    data = build()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    template = HTML_TEMPLATE.read_text(encoding="utf-8") if HTML_TEMPLATE.is_file() else ""
    if not template:
        raise SystemExit(f"ERROR: template not found: {HTML_TEMPLATE}")

    write_split_site(data, template)
    print(f"Deploy: drag the folder {SITE_DIR} to your host, or zip it with zip_site.py.")
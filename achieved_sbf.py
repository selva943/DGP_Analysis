"""achieved_sbf.py

Determine which previously identified Easy SBF gaps (from find_easy_sbf.py)
have been achieved by comparing the historical baseline against the current
SBD report.

Usage
-----
    python achieved_sbf.py                     AUTO   -> today - 2 days
    python achieved_sbf.py --date DD-MM-YYYY   MANUAL -> exact analysis date
    python achieved_sbf.py --history-file FILE -> use a specific history run
    python achieved_sbf.py --help              help text

Pipeline
--------
1. Resolve the Analysis Visit Date (AUTO = today - 2 calendar days,
   MANUAL = the --date value; the manual date always wins).
2. Scan History/Easy_SBF_Gap_*.xlsx and select the EARLIEST valid run whose
   Target Visit Date equals the analysis date (Run_Info.Target_Visit_Date is
   preferred; Easy_SBF_Gap.visit_date is the fallback). The earliest run is
   the pre-visit baseline; re-runs with refreshed SBD data never change it.
   Runs whose execution time is not before the current run are ignored.
   --history-file overrides the selection with a specific file.
3. Read the historical baseline: DSE + Retailer + Visit Date and the Easy SBF
   reasons that were identified BEFORE the visit.
4. Read every Input/SBD_report*.CSV, apply the SAME channel rule as
   find_easy_sbf.py (rows whose channel_description contains "WS" are
   EXCLUDED) and aggregate No Sale reasons at retailer level FIRST.
5. Compare the historical Easy SBF set against the current No Sale set while
   preserving the DSE + Retailer + Visit Date relationship.
6. Classify each historical Easy SBF as ACHIEVED (gone from current No Sale)
   or STILL OPEN (still present). SBFs in today's No Sale that are on the
   Easy SBF master but were not in the historical baseline are NEW.
7. Write a workbook (Output/ + History/) that is never overwritten.

Business rules
--------------
- DSE_CODE + RETAILER_CODE + VISIT_DATE is the business key. RetailerCode is
  NEVER treated as unique; SBD data is aggregated to retailer level only and
  then applied to every historical DSE + Retailer relationship.
- A retailer missing from today's SBD is NOT automatically "achieved"; such
  records are flagged NO_CURRENT_SBD.
- A retailer present in today's SBD with a blank No Sale means every
  historical Easy SBF is considered achieved (SBD_PRESENT_NO_SALE_CLEAR).
- NEW SBF is restricted to products on the Easy SBF master
  (updating_files/Easy_SBF_List.xlsx) so ordinary No Sale products are never
  miscounted as new Easy SBF gaps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "Input"
UPDATING_DIR = BASE_DIR / "updating_files"
OUTPUT_DIR = BASE_DIR / "Output"
HISTORY_DIR = BASE_DIR / "History"

EASY_SBF_FILE = UPDATING_DIR / "Easy_SBF_List.xlsx"
NAME_MASTER_FILE = UPDATING_DIR / "Name_master.xlsx"

SBD_PREFIX = "SBD_report"
SBD_SUFFIX = ".csv"

HISTORY_PREFIX = "Easy_SBF_Gap"
HISTORY_SUFFIX = ".xlsx"

OUTPUT_FILE_PREFIX = "Achieved_SBF"

CHANNEL_WS_PATTERN = "WS"

OUTPUT_COLUMNS = [
    "dse_code",
    "retailer_code",
    "visit_date",
    "historical_easy_sbf_gap",
    "achieved_sbf_gap",
    "achieved_sbf",
    "still_open_easy_sbf_gap",
    "still_open_easy_sbf",
    "new_sbf_gap",
    "new_sbf",
    "comparison_status",
]

BLANK_CODE_VALUES = {"", "NAN", "NONE", "NA", "N/A", "NULL", "<NA>", "NAT"}

COLUMN_ALIASES = {
    "user_code": ("user_code", "usercode"),
    "retailer_code": ("retailer_code", "retailercode"),
    "visit_date": ("visit_date", "vist_date", "visitdate", "vistdate"),
    "dse_code": ("dse_code", "dsecode"),
    "channel_description": ("channel_description", "channeldesc"),
    "no_sale": ("no_sale", "nosale"),
    "easy_sbf": ("easy_sbf", "easy_sbf_list", "easy_sbf_master", "easysbf"),
    "easy_sbf_gap": ("easy_sbf_gap", "easysbfgap"),
    "field": ("field", "field_name", "run_info_field"),
    "value": ("value", "value_name", "run_info_value"),
}

COLUMN_FALLBACKS = {
    "visit_date": (("visit",), ("vist",), ("date",)),
    "dse_code": (("dse",),),
    "channel_description": (("channel",),),
    "no_sale": (("no", "sale"), ("nosale",)),
    "easy_sbf": (("easy", "sbf"), ("sbf",)),
    "easy_sbf_gap": (("easy", "sbf", "gap"),),
}

DATE_ARG_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})$")

HELP_TEXT = """Achieved SBF Analysis

Usage:

python achieved_sbf.py
    Analyse the visit date whose result should now be available
    (today minus 2 calendar days).

python achieved_sbf.py --date DD-MM-YYYY
    Analyse the specified visit date.

python achieved_sbf.py --history-file FILE
    Use a specific History/Easy_SBF_Gap_*.xlsx file as the baseline,
    bypassing the earliest-run auto-selection.

Supported date formats:
    DD-MM-YYYY          e.g. 08-08-2026
    YYYY-MM-DD          e.g. 2026-08-08

The baseline is the EARLIEST run for the Analysis Visit Date (the pre-visit
snapshot); re-runs with refreshed SBD data never change the baseline.

Examples:

python achieved_sbf.py

python achieved_sbf.py --date 08-08-2026

python achieved_sbf.py --date 09-09-2026 --history-file Easy_SBF_Gap_20260909.xlsx
"""


class CommandError(Exception):
    """Fatal user/input error; the process exits with a non-zero code."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="ERROR: %(message)s",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# Normalization helpers (must stay compatible with find_easy_sbf.py)
# ---------------------------------------------------------------------------

def normalize_column_name(name: object) -> str:
    """Normalize a column name for tolerant lookup."""
    text = str(name).strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[\s\-./]+", "_", text)
    text = re.sub(r"[^a-zA-Z0-9_]", "", text)
    text = text.lower()
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def find_column(columns: Sequence[object], standard_key: str) -> Optional[str]:
    """Return the best matching raw column name for a standard key."""
    normalized = {normalize_column_name(c): c for c in columns}

    for alias in COLUMN_ALIASES.get(standard_key, ()):
        if alias in normalized:
            return normalized[alias]

    for tokens in COLUMN_FALLBACKS.get(standard_key, ()):
        for column in columns:
            if all(token in normalize_column_name(column) for token in tokens):
                return column

    return None


def required_column(df: pd.DataFrame, standard_key: str, source: str) -> str:
    """Resolve a standard column name or raise a helpful error."""
    column = find_column(list(df.columns), standard_key)
    if column is None:
        raise CommandError(
            f"Required column '{standard_key}' not found in {source}. "
            f"Available columns: {list(df.columns)}"
        )
    return column


def normalize_code(value: object) -> str:
    """Normalize a DSE or retailer code for matching."""
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
    return "" if text in BLANK_CODE_VALUES else text


def normalize_sbf_text(value: object) -> str:
    """Normalize an SBF reason for exact comparison.

    "No Demand(1)", "No Demand(2)" and "No Demand" all resolve to the same
    normalized value.
    """
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value)
    text = re.sub(r"\(\s*\d+\s*\)", "", text)
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^a-zA-Z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def clean_sbf_readable(value: object) -> str:
    """Return a readable version of an SBF reason (original casing)."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\(\s*\d+\s*\)", "", text)
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^a-zA-Z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_target_date(text: str) -> Optional[dt.date]:
    """Parse a date given as DD-MM-YYYY or YYYY-MM-DD."""
    text = str(text).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_history_date(value: object) -> Optional[dt.date]:
    """Parse a visit date stored in history (DD-MM-YYYY / YYYY-MM-DD)."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def read_excel(path: Path, label: str) -> pd.DataFrame:
    """Read an Excel file with graceful error reporting."""
    if not path.is_file():
        raise CommandError(f"{label} not found: {path}")
    try:
        return pd.read_excel(path)
    except Exception as exc:
        raise CommandError(f"Could not read {label} ({path}): {exc}") from exc


def _read_csv_any_encoding(path: Path, **kwargs) -> pd.DataFrame:
    """Read a CSV trying common encodings in order."""
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False, **kwargs)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            raise CommandError(f"Could not read CSV file {path.name}: {exc}") from exc
    raise CommandError(f"Could not decode CSV file {path.name}: {last_error}")


def load_easy_sbf_master() -> set[str]:
    """Load the Easy SBF master and return the normalized lookup set."""
    df = read_excel(EASY_SBF_FILE, "Easy SBF Master")
    column = required_column(df, "easy_sbf", EASY_SBF_FILE.name)
    master = {v for v in df[column].map(normalize_sbf_text) if v}
    if not master:
        raise CommandError("Easy SBF Master contains no valid SBF names.")
    return master


def load_name_master() -> set[str]:
    """Load the Name Master and return the set of valid DSE codes."""
    df = read_excel(NAME_MASTER_FILE, "Name Master")
    column = required_column(df, "dse_code", NAME_MASTER_FILE.name)
    valid = {c for c in df[column].map(normalize_code) if c}
    if not valid:
        raise CommandError("Name Master contains no valid DSE codes.")
    return valid


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history_files() -> list[Path]:
    """Return all Easy SBF history workbooks in History/."""
    if not HISTORY_DIR.is_dir():
        raise CommandError(f"Missing History folder: {HISTORY_DIR}")
    files = sorted(
        p
        for p in HISTORY_DIR.iterdir()
        if p.is_file()
        and p.name.lower().startswith(HISTORY_PREFIX.lower())
        and p.name.lower().endswith(HISTORY_SUFFIX.lower())
    )
    if not files:
        raise CommandError(
            f"No history files found in {HISTORY_DIR}. "
            f"Expected: {HISTORY_PREFIX}*{HISTORY_SUFFIX}"
        )
    return files


def _read_run_info(path: Path) -> dict:
    """Read the Run_Info sheet as a Field -> Value dict (or {} on failure)."""
    try:
        sheet = pd.read_excel(path, sheet_name="Run_Info")
    except Exception:
        return {}
    field_col = find_column(list(sheet.columns), "field")
    value_col = find_column(list(sheet.columns), "value")
    if field_col is None or value_col is None:
        return {}
    info: dict = {}
    for _, row in sheet.iterrows():
        key = str(row[field_col]).strip()
        if not key or key.lower() in ("nan", "none"):
            continue
        info[key] = row[value_col]
    return info


def _history_target_date(path: Path, run_info: dict) -> Optional[dt.date]:
    """Determine the Target Visit Date of a history workbook.

    Run_Info.Target_Visit_Date is the preferred identification method; the
    Easy_SBF_Gap.visit_date column is the fallback. The filename date is
    never trusted.
    """
    raw = run_info.get("Target_Visit_Date")
    if raw:
        parsed = parse_history_date(raw)
        if parsed:
            return parsed
    try:
        df = pd.read_excel(path, sheet_name="Easy_SBF_Gap")
        column = find_column(list(df.columns), "visit_date")
        if column is None:
            return None
        dates = {d for d in df[column].map(parse_history_date) if d}
        if len(dates) == 1:
            return dates.pop()
    except Exception as exc:
        logging.warning("Could not inspect history file %s: %s", path.name, exc)
    return None


def _history_execution_time(path: Path, run_info: dict) -> Optional[dt.datetime]:
    """Return the execution time of a history run (prefer Execution_Timestamp)."""
    raw_ts = run_info.get("Execution_Timestamp")
    if raw_ts:
        try:
            return dt.datetime.strptime(str(raw_ts).strip(), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    raw_date = run_info.get("Execution_Date")
    raw_time = run_info.get("Execution_Time")
    if raw_date and raw_time:
        try:
            return dt.datetime.strptime(
                f"{str(raw_date).strip()} {str(raw_time).strip()}",
                "%d-%m-%Y %H:%M:%S",
            )
        except ValueError:
            pass
    return None


def _file_mtime(path: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(path.stat().st_mtime)


def find_matching_history(
    files: list[Path],
    analysis_date: dt.date,
    now: dt.datetime,
    forced_file: Optional[Path] = None,
) -> tuple[Path, dict, dt.datetime]:
    """Select the earliest valid history run for the analysis date.

    The FIRST run for a target visit date is the pre-visit baseline (AUTO
    mode targets the next future Visit Date); re-runs with refreshed SBD
    data must never change that baseline, so the earliest execution time
    wins. Only runs executed strictly before the current run are considered.
    When run metadata is unavailable the file's last-modified time is used
    as a fallback. A forced_file (--history-file) bypasses all selection.
    """
    if forced_file is not None:
        if not forced_file.is_file():
            raise CommandError(f"History file not found: {forced_file}")
        return forced_file, _read_run_info(forced_file), dt.datetime.min

    candidates: list[tuple[dt.datetime, float, str, Path, dict]] = []
    for path in files:
        run_info = _read_run_info(path)
        target = _history_target_date(path, run_info)
        if target is None:
            logging.warning("Skipping history file (unreadable/ambiguous): %s", path.name)
            continue
        if target != analysis_date:
            continue
        exec_dt = _history_execution_time(path, run_info)
        if exec_dt is None:
            exec_dt = _file_mtime(path)
            logging.warning(
                "History file %s has no execution timestamp; using file time %s",
                path.name, exec_dt,
            )
        if exec_dt >= now:
            logging.warning(
                "Skipping history file %s: execution time %s is not before current run %s",
                path.name, exec_dt, now,
            )
            continue
        candidates.append((exec_dt, _file_mtime(path), path.name, path, run_info))

    if not candidates:
        raise CommandError(
            f"No Easy SBF history found for analysis date "
            f"{analysis_date.strftime('%d-%m-%Y')} in {HISTORY_DIR}."
        )

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    _, _, _, earliest_path, earliest_run = candidates[0]
    return earliest_path, earliest_run, candidates[0][0]


def load_history_workbook(path: Path) -> tuple[pd.DataFrame, dict]:
    """Load the historical baseline: DSE + Retailer + Visit Date + Easy SBF."""
    try:
        df = pd.read_excel(path, sheet_name="Easy_SBF_Gap")
        run_info = _read_run_info(path)
    except Exception as exc:
        raise CommandError(f"Could not read history workbook {path.name}: {exc}") from exc

    if df.empty:
        raise CommandError(f"History workbook is empty: {path.name}")

    out = pd.DataFrame(
        {
            "dse_code": df[required_column(df, "dse_code", path.name)].map(normalize_code),
            "retailer_code": df[required_column(df, "retailer_code", path.name)].map(normalize_code),
            "easy_sbf_gap": pd.to_numeric(
                df[required_column(df, "easy_sbf_gap", path.name)], errors="coerce"
            )
            .fillna(0)
            .astype(int),
            "easy_sbf": df[required_column(df, "easy_sbf", path.name)],
        }
    )
    visit_dates = df[required_column(df, "visit_date", path.name)].map(parse_history_date)
    out["visit_date"] = visit_dates
    out["visit_date_str"] = visit_dates.map(lambda d: d.strftime("%d-%m-%Y") if d else "")

    dropped = out[(out["dse_code"] == "") | (out["retailer_code"] == "") | out["visit_date"].isna()]
    if not dropped.empty:
        logging.warning("Dropped %s history rows with blank codes or invalid visit date.", len(dropped))
    out = out[(out["dse_code"] != "") & (out["retailer_code"] != "") & out["visit_date"].notna()].copy()
    out = out.drop_duplicates(subset=["dse_code", "retailer_code", "visit_date"], keep="first")

    if out.empty:
        raise CommandError(f"History workbook contains no valid baseline rows: {path.name}")

    dates_present = {d for d in out["visit_date"] if d}
    if len(dates_present) > 1:
        logging.warning(
            "History workbook %s contains mixed visit dates; all rows will be analysed.",
            path.name,
        )
    return out, run_info


# ---------------------------------------------------------------------------
# Current SBD
# ---------------------------------------------------------------------------

def discover_sbd_reports() -> list[Path]:
    """Return all SBD report files (case-insensitive name match)."""
    if not INPUT_DIR.is_dir():
        raise CommandError(f"Missing Input folder: {INPUT_DIR}")
    files = sorted(
        p
        for p in INPUT_DIR.iterdir()
        if p.is_file()
        and p.name.lower().startswith(SBD_PREFIX.lower())
        and p.name.lower().endswith(SBD_SUFFIX.lower())
    )
    if not files:
        raise CommandError(
            f"No SBD report files found in {INPUT_DIR}. "
            f"Expected: {SBD_PREFIX}*{SBD_SUFFIX}"
        )
    return files


def read_sbd_report(path: Path) -> pd.DataFrame:
    """Read one SBD report and keep only the required columns."""
    header = _read_csv_any_encoding(path, nrows=0)
    raw_columns = list(header.columns)

    mapping: dict[str, str] = {}
    for standard_key in ("retailer_code", "channel_description", "no_sale"):
        raw = find_column(raw_columns, standard_key)
        if raw is None:
            raise CommandError(
                f"SBD report {path.name} is missing required column "
                f"'{standard_key}'. Available columns: {raw_columns}"
            )
        mapping[standard_key] = raw

    df = _read_csv_any_encoding(path, usecols=list(mapping.values()))
    df = df.rename(columns={raw: std for std, raw in mapping.items()})
    df["retailer_code"] = df["retailer_code"].map(normalize_code)
    return df


def load_sbd_reports() -> tuple[pd.DataFrame, list[Path]]:
    """Read and merge every SBD report into a single dataframe."""
    files = discover_sbd_reports()
    frames: list[pd.DataFrame] = []
    for path in files:
        frame = read_sbd_report(path)
        if frame.empty:
            logging.warning("SBD report is empty: %s", path.name)
        else:
            frames.append(frame)

    if not frames:
        raise CommandError("All SBD reports are empty or missing required columns.")
    return pd.concat(frames, ignore_index=True, sort=False), files


def filter_non_ws_sbd(sbd: pd.DataFrame) -> pd.DataFrame:
    """Exclude SBD rows whose channel description contains "WS".

    This is the SAME rule as find_easy_sbf.py: WS / Wholesale rows are
    dropped and every non-WS channel is kept.
    """
    non_ws = sbd[
        ~sbd["channel_description"].astype(str).str.contains(
            CHANNEL_WS_PATTERN, case=False, na=False
        )
    ].copy()
    return non_ws[non_ws["retailer_code"] != ""]


def _join_no_sale(values) -> str:
    """Join all non-blank No Sale values of a group with '|'."""
    parts = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, float) and pd.isna(value):
            continue
        text = str(value).strip()
        if text:
            parts.append(text)
    return "|".join(parts)


def aggregate_current_sbd(non_ws_sbd: pd.DataFrame) -> pd.DataFrame:
    """Aggregate today's No Sale data to retailer_code level FIRST.

    This prevents any many-to-many merge that would multiply DSE + Retailer
    relationships or inflate SBF counts.
    """
    if non_ws_sbd.empty:
        return pd.DataFrame(columns=["retailer_code", "_no_sale"])
    grouped = (
        non_ws_sbd.groupby("retailer_code")["no_sale"]
        .agg(_join_no_sale)
        .reset_index()
        .rename(columns={"no_sale": "_no_sale"})
    )
    return grouped


def split_current_no_sale(raw: object) -> list[tuple[str, str]]:
    """Split a current No Sale cell on '|' into (normalized, readable) reasons."""
    if raw is None:
        return []
    if isinstance(raw, float) and pd.isna(raw):
        return []
    reasons: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in str(raw).split("|"):
        norm = normalize_sbf_text(part)
        if norm and norm not in seen:
            seen.add(norm)
            reasons.append((norm, clean_sbf_readable(part)))
    return reasons


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

def parse_historical_easy_sbf(raw: object) -> list[tuple[str, str]]:
    """Split a historical Easy SBF cell on commas into (normalized, readable)."""
    if raw is None:
        return []
    if isinstance(raw, float) and pd.isna(raw):
        return []
    reasons: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in str(raw).split(","):
        norm = normalize_sbf_text(part)
        if norm and norm not in seen:
            seen.add(norm)
            reasons.append((norm, clean_sbf_readable(part)))
    return reasons


def compare_sbf_sets(
    historical: set[str], current: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """Return (achieved, still_open, current_minus_historical) as normalized sets."""
    achieved = historical - current
    still_open = historical & current
    current_only = current - historical
    return achieved, still_open, current_only


def calculate_achievement(
    historical_parsed: list[tuple[str, str]],
    current_parsed: list[tuple[str, str]],
    has_current: bool,
    master_set: set[str],
) -> dict:
    """Classify one DSE + Retailer + Visit Date row into achieved/still_open/new.

    historical_easy_sbf_gap is derived from the distinct historical Easy SBF
    set so it always equals achieved_sbf_gap + still_open_easy_sbf_gap
    (achieved and still open partition the historical set).
    """
    historical_norms = {norm for norm, _ in historical_parsed}
    current_norms = {norm for norm, _ in current_parsed}
    historical_readable = dict(historical_parsed)
    current_readable = dict(current_parsed)

    if not has_current:
        achieved_norms: set[str] = set()
        still_open_norms: set[str] = set(historical_norms)
        new_norms: set[str] = set()
        status = "NO_CURRENT_SBD"
    elif not current_norms:
        achieved_norms = set(historical_norms)
        still_open_norms = set()
        new_norms = set()
        status = "SBD_PRESENT_NO_SALE_CLEAR"
    else:
        achieved_norms, still_open_norms, current_only = compare_sbf_sets(
            historical_norms, current_norms
        )
        new_norms = {norm for norm in current_only if norm in master_set}
        status = "NO_HISTORICAL_EASY_SBF" if not historical_norms else "SBD_MATCHED"

    achieved_sbf = ", ".join(
        historical_readable[norm] for norm, _ in historical_parsed if norm in achieved_norms
    )
    still_open_sbf = ", ".join(
        historical_readable[norm] for norm, _ in historical_parsed if norm in still_open_norms
    )
    new_sbf = ", ".join(
        current_readable[norm] for norm, _ in current_parsed if norm in new_norms
    )

    return {
        "historical_easy_sbf_gap": len(historical_norms),
        "achieved_sbf_gap": len(achieved_norms),
        "achieved_sbf": achieved_sbf,
        "still_open_easy_sbf_gap": len(still_open_norms),
        "still_open_easy_sbf": still_open_sbf,
        "new_sbf_gap": len(new_norms),
        "new_sbf": new_sbf,
        "comparison_status": status,
    }


def build_output(
    history_df: pd.DataFrame,
    current_map: dict[str, list[tuple[str, str]]],
    master_set: set[str],
) -> pd.DataFrame:
    """Build one output row per historical DSE + Retailer + Visit Date."""
    rows: list[dict] = []
    for _, row in history_df.iterrows():
        retailer = row["retailer_code"]
        historical_parsed = parse_historical_easy_sbf(row["easy_sbf"])
        stored_gap = int(row["easy_sbf_gap"])
        if len(historical_parsed) != stored_gap:
            logging.warning(
                "History row %s | %s | %s has stored easy_sbf_gap=%s but %s "
                "distinct SBF name(s); using the distinct count for the analysis.",
                row["dse_code"], retailer, row["visit_date_str"], stored_gap,
                len(historical_parsed),
            )
        current_parsed = current_map.get(retailer, [])
        result = calculate_achievement(
            historical_parsed=historical_parsed,
            current_parsed=current_parsed,
            has_current=retailer in current_map,
            master_set=master_set,
        )
        rows.append(
            {
                "dse_code": row["dse_code"],
                "retailer_code": retailer,
                "visit_date": row["visit_date_str"],
                **result,
            }
        )
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if not output.empty:
        output = output.sort_values(
            ["dse_code", "retailer_code", "visit_date"], ignore_index=True
        )
    return output


def validate_results(output_df: pd.DataFrame) -> int:
    """Check historical_easy_sbf_gap == achieved + still_open for every row.

    New SBF is intentionally not part of this equation. Returns the number of
    rows that violated the formula.
    """
    if output_df.empty:
        return 0
    violations = 0
    for idx, row in output_df.iterrows():
        expected = int(row["historical_easy_sbf_gap"])
        actual = int(row["achieved_sbf_gap"]) + int(row["still_open_easy_sbf_gap"])
        if expected != actual:
            violations += 1
            logging.warning(
                "Validation failed for %s | %s | %s: historical=%s but "
                "achieved(%s) + still_open(%s) = %s",
                row["dse_code"],
                row["retailer_code"],
                row["visit_date"],
                expected,
                row["achieved_sbf_gap"],
                row["still_open_easy_sbf_gap"],
                actual,
            )
    if violations:
        logging.warning("Validation formula failed for %s row(s).", violations)
    return violations


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def next_available_name(base: str) -> str:
    """Return a filename base that exists in neither Output nor History."""
    ext = ".xlsx"
    if not (OUTPUT_DIR / f"{base}{ext}").exists() and not (HISTORY_DIR / f"{base}{ext}").exists():
        return base
    counter = 1
    while True:
        candidate = f"{base}_{counter}"
        if not (OUTPUT_DIR / f"{candidate}{ext}").exists() and not (
            HISTORY_DIR / f"{candidate}{ext}"
        ).exists():
            return candidate
        counter += 1


def write_run_info(writer: pd.ExcelWriter, run_info: dict) -> None:
    info = pd.DataFrame({"Field": list(run_info.keys()), "Value": list(run_info.values())})
    info.to_excel(writer, sheet_name="Run_Info", index=False)


def save_workbook(path: Path, output_df: pd.DataFrame, run_info: dict) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        output_df.to_excel(writer, sheet_name="Achieved_SBF", index=False)
        write_run_info(writer, run_info)


def save_output(output_df: pd.DataFrame, run_info: dict) -> tuple[Path, Path]:
    """Write to Output/ and History/, never overwriting previous analyses."""
    name = next_available_name(f"{OUTPUT_FILE_PREFIX}_{run_info['Execution_Timestamp']}")
    output_path = OUTPUT_DIR / f"{name}.xlsx"
    history_path = HISTORY_DIR / f"{name}.xlsx"
    try:
        save_workbook(output_path, output_df, run_info)
        save_workbook(history_path, output_df, run_info)
    except PermissionError as exc:
        raise CommandError(f"Permission denied while writing output files: {exc}") from exc
    except Exception as exc:
        raise CommandError(f"Failed to write Excel output files: {exc}") from exc
    return output_path, history_path


def relpath(path: Path) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def parse_analysis_date(
    manual_text: Optional[str], today: dt.date
) -> tuple[dt.date, str]:
    """Resolve the Analysis Visit Date.

    Manual --date always wins; otherwise today minus 2 calendar days.
    """
    if manual_text:
        parsed = parse_target_date(manual_text)
        if parsed is None:
            raise CommandError(
                f"Invalid analysis date: {manual_text}\n"
                "Expected format: DD-MM-YYYY (or YYYY-MM-DD)"
            )
        return parsed, "MANUAL"
    return today - dt.timedelta(days=2), "AUTO"


def run(args: argparse.Namespace) -> None:
    ensure_directories()

    print("=" * 50)
    print("       ACHIEVED SBF ANALYSIS")
    print("=" * 50)

    now = dt.datetime.now()
    execution_dt = now
    timestamp_str = execution_dt.strftime("%Y%m%d_%H%M%S")
    today = dt.date.today()

    analysis_date, date_mode = parse_analysis_date(args.date, today)

    print()
    print(f"Execution Date      : {execution_dt.strftime('%d-%m-%Y')}")
    print(f"Analysis Visit Date : {analysis_date.strftime('%d-%m-%Y')}")
    print(f"Date Mode           : {date_mode}")
    print()
    print("Loading source files...")

    master_set = load_easy_sbf_master()
    print("Easy SBF Master     : OK")

    valid_dse_set = load_name_master()
    print("Name Master         : OK")

    print()
    print("-" * 50)
    print("HISTORY")
    print("-" * 50)

    history_files = load_history_files()

    forced_history = None
    if args.history_file is not None:
        forced_history = HISTORY_DIR / args.history_file
    print(f"History Files Found : {len(history_files)}")

    history_path, history_run_info, history_exec = find_matching_history(
        history_files, analysis_date, now, forced_history
    )
    print(f"Selected History    : {history_path.name}")

    history_df, _ = load_history_workbook(history_path)
    print(f"Historical Rows     : {len(history_df)}")

    unknown_dses = sorted(
        {d for d in history_df["dse_code"] if d not in valid_dse_set}
    )
    if unknown_dses:
        logging.warning(
            "%s historical DSE code(s) not present in Name Master (first: %s).",
            len(unknown_dses), ", ".join(unknown_dses[:5]),
        )

    print()
    print("-" * 50)
    print("CURRENT SBD")
    print("-" * 50)

    sbd, sbd_files = load_sbd_reports()
    print(f"SBD Files           : {len(sbd_files)}")

    sbd_rows = len(sbd)
    sbd_non_ws = filter_non_ws_sbd(sbd)
    non_ws_rows = len(sbd_non_ws)

    print(f"SBD Rows            : {sbd_rows}")
    print(f"Non-WS Rows         : {non_ws_rows}")

    current_agg = aggregate_current_sbd(sbd_non_ws)
    current_map: dict[str, list[tuple[str, str]]] = {}
    for _, r in current_agg.iterrows():
        current_map[r["retailer_code"]] = split_current_no_sale(r["_no_sale"])

    history_retailers = set(history_df["retailer_code"])
    retailers_matched = len(history_retailers & set(current_map))
    print(f"Retailers Matched   : {retailers_matched}")

    print()
    print("-" * 50)
    print("RESULT")
    print("-" * 50)

    print("Comparing sets...")
    output_df = build_output(history_df, current_map, master_set)
    violations = validate_results(output_df)

    total_historical = int(output_df["historical_easy_sbf_gap"].sum()) if not output_df.empty else 0
    total_achieved = int(output_df["achieved_sbf_gap"].sum()) if not output_df.empty else 0
    total_still_open = int(output_df["still_open_easy_sbf_gap"].sum()) if not output_df.empty else 0
    total_new = int(output_df["new_sbf_gap"].sum()) if not output_df.empty else 0

    print(f"Output Rows         : {len(output_df)}")
    print(f"Historical Easy SBF : {total_historical}")
    print(f"Achieved SBF        : {total_achieved}")
    print(f"Still Open SBF      : {total_still_open}")
    print(f"New SBF             : {total_new}")
    if violations:
        print(f"Validation Warnings : {violations}")

    run_info = {
        "Execution_Date": execution_dt.strftime("%d-%m-%Y"),
        "Execution_Time": execution_dt.strftime("%H:%M:%S"),
        "Execution_Timestamp": timestamp_str,
        "Analysis_Visit_Date": analysis_date.strftime("%d-%m-%Y"),
        "Date_Mode": date_mode,
        "History_File_Used": history_path.name,
        "History_Target_Visit_Date": analysis_date.strftime("%d-%m-%Y"),
        "SBD_Files_Processed": len(sbd_files),
        "Total_SBD_Rows": sbd_rows,
        "Non_WS_SBD_Rows": non_ws_rows,
        "Retailers_Matched": retailers_matched,
        "Total_History_Rows": len(history_df),
        "Total_Output_Rows": len(output_df),
        "Total_Achieved_Gap": total_achieved,
        "Total_Still_Open_Gap": total_still_open,
        "Total_New_SBF_Gap": total_new,
    }

    output_path, history_out_path = save_output(output_df, run_info)

    print()
    print("-" * 50)
    print("OUTPUT")
    print("-" * 50)
    print(f"Output File         : {relpath(output_path)}")
    print(f"Analysis History    : {relpath(history_out_path)}")

    print()
    print("=" * 50)
    print("PROCESS COMPLETED")
    print("=" * 50)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that turns usage errors into friendly command errors."""

    def error(self, message: str) -> None:
        raise CommandError(
            f"{message}\nRun 'python achieved_sbf.py --help' for usage."
        )


def _date_without_flag(argv: Sequence[str]) -> Optional[str]:
    """Return an argument that looks like a date but was not passed via --date."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--date":
            i += 2
            continue
        if token.startswith("--"):
            candidate = token[2:]
        elif not token.startswith("-") and token:
            candidate = token
        else:
            candidate = None
        if candidate and DATE_ARG_PATTERN.match(candidate) and parse_target_date(candidate):
            return token
        i += 1
    return None


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)

    bad_date = _date_without_flag(argv)
    if bad_date:
        value = bad_date[2:] if bad_date.startswith("--") else bad_date
        raise CommandError(
            f"Invalid argument '{bad_date}'.\n"
            "An analysis date must be passed with the --date flag.\n"
            f"Did you mean:  python achieved_sbf.py --date {value}"
        )

    parser = _ArgumentParser(
        add_help=False,
        description="Achieved SBF Analysis.",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="DD-MM-YYYY",
        help="Analyse the specified Visit Date (DD-MM-YYYY or YYYY-MM-DD).",
    )
    parser.add_argument(
        "--history-file",
        default=None,
        metavar="FILE",
        help="Use a specific History/Easy_SBF_Gap_*.xlsx file instead of "
        "auto-selecting the earliest run for the analysis date.",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    args = parser.parse_args(argv)
    if args.show_help:
        print(HELP_TEXT)
        raise SystemExit(0)
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
    try:
        args = parse_args(argv)
        run(args)
        return 0
    except CommandError as exc:
        logging.error("%s", exc)
        return 1
    except SystemExit:
        raise
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

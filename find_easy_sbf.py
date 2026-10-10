"""find_easy_sbf.py

Identify Easy SBF (Sales Blocking Factors) gaps for retailers by combining
the Calls Plan, SBD reports, Easy SBF Master and DSE Name Master.

Usage
-----
    python find_easy_sbf.py                     AUTO   -> next future Visit Date
    python find_easy_sbf.py --date DD-MM-YYYY   MANUAL -> exact Visit Date
    python find_easy_sbf.py --help              help text

Pipeline
--------
1. Read and merge every Input/SBD_report*.CSV.
2. Load updating_files/Calls_plan.xlsx, Easy_SBF_List.xlsx, Name_master.xlsx.
3. Normalize column names, DSE/retailer codes and SBF text.
4. Determine the target Visit Date (AUTO = MIN(Visit_date > today),
   MANUAL = the --date value).
5. Keep only target-date Calls Plan rows whose DSE exists in Name Master;
   remove only exact duplicate DSE + Retailer + Visit Date relationships.
6. Exclude SBD rows whose channel description contains "WS"; keep the
   remaining (non-WS) channels.
7. Aggregate SBD No Sale reasons at retailer level, then apply to every
   valid DSE + Retailer relationship (never merging a retailer across DSEs).
8. Split No Sale reasons, count total gaps and Easy SBF gaps.
9. Write a workbook named after the target Visit Date (e.g.
   Easy_SBF_Gap_20260810.xlsx) to Output/ and a permanent copy in History/,
   never overwriting previous results.

Business rule
-------------
A retailer may be assigned to multiple DSEs. The unique business key is:

    DSE_CODE + RETAILER_CODE + VISIT_DATE

RetailerCode is NEVER treated as unique. SBD reports are aggregated only to
retailer level (Layer 2) and then merged into the DSE + Retailer mapping
(Layer 1) so no many-to-many multiplication can occur. The final output is
always at DSE + Retailer + Visit Date level (Layer 3).
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

CALLS_PLAN_FILE = UPDATING_DIR / "Calls_plan.xlsx"
NAME_MASTER_FILE = UPDATING_DIR / "Name_master.xlsx"
EASY_SBF_FILE = UPDATING_DIR / "Easy_SBF_List.xlsx"

SBD_PREFIX = "SBD_report"
SBD_SUFFIX = ".csv"

OUTPUT_FILE_PREFIX = "Easy_SBF_Gap"
# WS channel rule: SBD rows whose channel_description contains
# CHANNEL_WS_PATTERN are EXCLUDED; every non-WS channel is kept.
CHANNEL_WS_PATTERN = "WS"
OUTPUT_COLUMNS = [
    "dse_code",
    "retailer_code",
    "visit_date",
    "total_gap",
    "easy_sbf_gap",
    "easy_sbf",
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
}

COLUMN_FALLBACKS = {
    "visit_date": (("visit",), ("vist",), ("date",)),
    "dse_code": (("dse",),),
    "channel_description": (("channel",),),
    "no_sale": (("no", "sale"), ("nosale",)),
    "easy_sbf": (("easy", "sbf"), ("sbf",)),
}

DATE_ARG_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})$")

HELP_TEXT = """Easy SBF Gap Analysis

Usage:

python find_easy_sbf.py
    Automatically use the next available future Visit Date.

python find_easy_sbf.py --date DD-MM-YYYY
    Process the specified Visit Date.

Supported date formats:
    DD-MM-YYYY          e.g. 08-08-2026
    YYYY-MM-DD          e.g. 2026-08-08

Examples:

python find_easy_sbf.py

python find_easy_sbf.py --date 08-08-2026
"""


class CommandError(Exception):
    """Fatal user/input error; the process exits with a non-zero code."""


class Stopped(Exception):
    """Scheduled safe stop without producing output."""


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
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_column_name(name: object) -> str:
    """Normalize a column name for tolerant lookup.

    Handles camelCase, spaces, underscores, hyphens, dots and capitalization:
    "Retailer Code", "Retailer_Code", "RETAILER_CODE", "RetailerCode"
    all normalize to "retailer_code".
    """
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
    """Normalize a DSE or retailer code for matching.

    Converts to string, trims spaces, removes hidden characters and
    whitespace, maps hyphens to underscores, strips stray punctuation,
    uppercases, and keeps meaningful underscore characters.
    """
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

    Removes numeric extensions like "(1)" / "(25)", collapses whitespace,
    maps underscores/hyphens to spaces, strips special characters and
    uppercases.  "No Demand", "NO DEMAND", "no_demand" and "No-Demand"
    all normalize to "NO DEMAND".
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


def coerce_datetime(series: pd.Series) -> pd.Series:
    """Convert a series to datetime, tolerant of common Excel formats."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="D", origin="1899-12-30", errors="coerce")
    return pd.to_datetime(series, dayfirst=True, errors="coerce", format="mixed")


def parse_target_date(text: str) -> Optional[dt.date]:
    """Parse a target date given as DD-MM-YYYY or YYYY-MM-DD."""
    text = str(text).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
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


def load_calls_plan() -> pd.DataFrame:
    """Load and normalize the Calls Plan file."""
    df = read_excel(CALLS_PLAN_FILE, "Calls Plan")
    if df.empty:
        raise CommandError(f"Calls Plan is empty: {CALLS_PLAN_FILE.name}")

    out = pd.DataFrame(
        {
            "user_code": df[required_column(df, "user_code", CALLS_PLAN_FILE.name)],
            "retailer_code": df[required_column(df, "retailer_code", CALLS_PLAN_FILE.name)],
            "visit_date": coerce_datetime(
                df[required_column(df, "visit_date", CALLS_PLAN_FILE.name)]
            ),
        }
    )
    out = out[out["visit_date"].notna()].copy()
    out["visit_date"] = out["visit_date"].dt.normalize()
    if out.empty:
        raise CommandError("Calls Plan contains no valid Visit dates.")
    return out


def load_name_master() -> set[str]:
    """Load the Name Master and return the set of valid DSE codes."""
    df = read_excel(NAME_MASTER_FILE, "Name Master")
    column = required_column(df, "dse_code", NAME_MASTER_FILE.name)
    valid = {c for c in df[column].map(normalize_code) if c}
    if not valid:
        raise CommandError("Name Master contains no valid DSE codes.")
    return valid


def load_easy_sbf_master() -> set[str]:
    """Load the Easy SBF master and return the normalized lookup set."""
    df = read_excel(EASY_SBF_FILE, "Easy SBF Master")
    column = required_column(df, "easy_sbf", EASY_SBF_FILE.name)
    return {v for v in df[column].map(normalize_sbf_text) if v}


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


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def determine_target_date(cp: pd.DataFrame, today: dt.date) -> Optional[dt.date]:
    """Return MIN(Visit_date WHERE Visit_date > today), or None."""
    future = [d for d in sorted(cp["visit_date"].dropna().unique()) if d > pd.Timestamp(today)]
    if not future:
        return None
    return future[0].date()


def filter_calls_plan(cp: pd.DataFrame, target_date: dt.date) -> pd.DataFrame:
    """Keep only Calls Plan records for the exact target Visit Date."""
    return cp[cp["visit_date"] == pd.Timestamp(target_date)].copy()


def filter_valid_dses(cp_target: pd.DataFrame, valid_dse_set: set[str]) -> pd.DataFrame:
    """Keep only DSEs present in Name Master and drop blank codes."""
    cp_target = cp_target.copy()
    cp_target["dse_code"] = cp_target["user_code"].map(normalize_code)
    cp_target["retailer_code"] = cp_target["retailer_code"].map(normalize_code)
    cp_target = cp_target[(cp_target["dse_code"] != "") & (cp_target["retailer_code"] != "")]
    return cp_target[cp_target["dse_code"].isin(valid_dse_set)].copy()


def dedupe_exact_relationships(cp_valid: pd.DataFrame) -> pd.DataFrame:
    """Remove ONLY exact duplicate DSE + Retailer + Visit Date relationships.

    Identical rows such as
        SM12271 | SEMDU_49061 | 08-08-2026
        SM12271 | SEMDU_49061 | 08-08-2026
    collapse to a single logical record, while
        SM12271 | SEMDU_49061 | 08-08-2026
        SM12272 | SEMDU_49061 | 08-08-2026
    remain two records (the retailer is NOT a unique key).
    """
    return cp_valid.drop_duplicates(
        subset=["dse_code", "retailer_code", "visit_date"], keep="first"
    ).copy()


def filter_non_ws_sbd(sbd: pd.DataFrame) -> pd.DataFrame:
    """Exclude SBD rows whose channel description contains "WS".

    Only non-WS channels (Small / Large / Convenience / Pharmacy / etc.)
    remain; WS / Wholesale rows are dropped.
    """
    non_ws = sbd[~sbd["channel_description"].astype(str).str.contains(CHANNEL_WS_PATTERN, case=False, na=False)].copy()
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


def match_retailers(valid_cp: pd.DataFrame, non_ws_sbd: pd.DataFrame) -> pd.DataFrame:
    """Join valid DSE + Retailer Calls Plan rows to non-WS SBD data.

    Layer 2: SBD No Sale reasons are aggregated to retailer_code level FIRST.
    Layer 3: that retailer-level summary is then merged into the DSE + Retailer
    mapping so every planned DSE + Retailer relationship receives the same
    retailer SBF info WITHOUT many-to-many multiplication.
    """
    if non_ws_sbd.empty:
        return pd.DataFrame(columns=["dse_code", "retailer_code", "_no_sale"])
    grouped = (
        non_ws_sbd.groupby("retailer_code")["no_sale"]
        .agg(_join_no_sale)
        .reset_index()
        .rename(columns={"no_sale": "_no_sale"})
    )
    return valid_cp.merge(grouped, on="retailer_code", how="inner")


def split_no_sale_reasons(raw: object) -> list[tuple[str, str]]:
    """Split a No Sale cell on '|' into (normalized, readable) reasons."""
    if raw is None:
        return []
    if isinstance(raw, float) and pd.isna(raw):
        return []
    reasons: list[tuple[str, str]] = []
    for part in str(raw).split("|"):
        norm = normalize_sbf_text(part)
        if norm:
            reasons.append((norm, clean_sbf_readable(part)))
    return reasons


def match_easy_sbf(reasons: list[tuple[str, str]], easy_sbf_set: set[str]) -> tuple[int, str]:
    """Return (easy_sbf_gap, comma-separated unique Easy SBF names)."""
    matched = [reason for reason in reasons if reason[0] in easy_sbf_set]
    seen: set[str] = set()
    unique: list[str] = []
    for norm, readable in matched:
        if norm not in seen:
            seen.add(norm)
            unique.append(readable)
    return len(matched), ", ".join(unique)


def build_output(
    merged: pd.DataFrame, target_date: dt.date, easy_sbf_set: set[str]
) -> pd.DataFrame:
    """Build the final output (one row per DSE + Retailer + Visit Date).

    The composite relationship DSE + Retailer is preserved exactly as it
    appears in the Calls Plan; a retailer shared by multiple DSEs yields one
    output row per DSE.
    """
    rows: list[dict] = []
    for _, row in merged.iterrows():
        reasons = split_no_sale_reasons(row["_no_sale"])
        easy_gap, easy_text = match_easy_sbf(reasons, easy_sbf_set)
        rows.append(
            {
                "dse_code": row["dse_code"],
                "retailer_code": row["retailer_code"],
                "visit_date": target_date.strftime("%d-%m-%Y"),
                "total_gap": len(reasons),
                "easy_sbf_gap": easy_gap,
                "easy_sbf": easy_text,
            }
        )
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if not output.empty:
        output = output.sort_values(
            ["dse_code", "retailer_code", "visit_date"], ignore_index=True
        )
    return output


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
        output_df.to_excel(writer, sheet_name="Easy_SBF_Gap", index=False)
        write_run_info(writer, run_info)


def save_history(path: Path, output_df: pd.DataFrame, run_info: dict) -> None:
    save_workbook(path, output_df, run_info)


def save_output(output_df: pd.DataFrame, run_info: dict) -> tuple[Path, Path]:
    """Write to Output/ and History/, preserving previous history files.

    The filename carries the TARGET VISIT DATE (e.g. Easy_SBF_Gap_20260810),
    not the execution time. Re-runs for the same date get a _1, _2 ... suffix
    so no previous result is ever overwritten.
    """
    name = next_available_name(f"{OUTPUT_FILE_PREFIX}_{run_info['File_Date_Stamp']}")
    output_path = OUTPUT_DIR / f"{name}.xlsx"
    history_path = HISTORY_DIR / f"{name}.xlsx"
    try:
        save_workbook(output_path, output_df, run_info)
        save_history(history_path, output_df, run_info)
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

def run(args: argparse.Namespace) -> None:
    ensure_directories()

    print("=" * 50)
    print("       EASY SBF GAP ANALYSIS")
    print("=" * 50)

    execution_dt = dt.datetime.now()
    timestamp_str = execution_dt.strftime("%Y%m%d_%H%M%S")

    manual_date = parse_target_date(args.date) if args.date else None
    if args.date and manual_date is None:
        raise CommandError(
            f"Invalid target date: {args.date}\n"
            "Expected format: DD-MM-YYYY (or YYYY-MM-DD)"
        )

    print()
    print(f"Execution Timestamp : {execution_dt.strftime('%d-%m-%Y %H:%M:%S')}")
    print(f"Date Mode           : {'MANUAL' if manual_date else 'AUTO'}")
    print()
    print("Loading source files...")

    calls = load_calls_plan()
    print("Calls Plan          : OK")

    valid_dse_set = load_name_master()
    print("Name Master         : OK")

    easy_sbf_set = load_easy_sbf_master()
    print("Easy SBF Master     : OK")

    sbd, sbd_files = load_sbd_reports()
    print(f"SBD Reports         : {len(sbd_files)} files found")

    print()
    print("-" * 50)
    print("DATE TARGETING")
    print("-" * 50)

    today = dt.date.today()
    if manual_date is not None:
        target_date = manual_date
        print(f"Target Visit Date   : {target_date.strftime('%d-%m-%Y')}  (manual)")
    else:
        print(f"System Date         : {today.strftime('%d-%m-%Y')}")
        target_date = determine_target_date(calls, today)
        if target_date is None:
            print("Searching Calls Plan...")
            print("No future Visit Date available.")
            print(f"Expected: Visit_date > {today.strftime('%d-%m-%Y')}")
            raise Stopped()
        print(f"Target Visit Date   : {target_date.strftime('%d-%m-%Y')}")

    print()
    print("-" * 50)
    print("PROCESSING")
    print("-" * 50)

    calls_target = filter_calls_plan(calls, target_date)
    print(f"Calls Plan Records  : {len(calls_target)}")
    if calls_target.empty:
        print(f"Target Visit Date : {target_date.strftime('%d-%m-%Y')}")
        print("No Calls Plan records found for this date.")
        raise Stopped()

    calls_valid = filter_valid_dses(calls_target, valid_dse_set)
    print(f"Valid DSE Records   : {len(calls_valid)}")

    calls_dedup = dedupe_exact_relationships(calls_valid)
    print(f"Exact-Dup Removed   : {len(calls_valid) - len(calls_dedup)}")

    sbd_non_ws = filter_non_ws_sbd(sbd)
    print(f"Non-WS SBD Records  : {len(sbd_non_ws)}")

    merged = match_retailers(calls_dedup, sbd_non_ws)
    print(f"Retailers Matched   : {len(merged)}")

    print("Processing No Sale...")
    print("Matching Easy SBF...")

    output_df = build_output(merged, target_date, easy_sbf_set)

    print()
    print("-" * 50)
    print("RESULT")
    print("-" * 50)
    print(f"Output Rows         : {len(output_df)}")
    total_gap = int(output_df["total_gap"].sum()) if not output_df.empty else 0
    easy_sbf_gap = int(output_df["easy_sbf_gap"].sum()) if not output_df.empty else 0
    print(f"Total Gap           : {total_gap}")
    print(f"Easy SBF Gap        : {easy_sbf_gap}")

    run_info = {
        "Execution_Date": execution_dt.strftime("%d-%m-%Y"),
        "Execution_Time": execution_dt.strftime("%H:%M:%S"),
        "Execution_Timestamp": timestamp_str,
        "Target_Visit_Date": target_date.strftime("%d-%m-%Y"),
        "File_Date_Stamp": target_date.strftime("%Y%m%d"),
        "Date_Mode": "MANUAL" if manual_date is not None else "AUTO",
        "SBD_Files_Processed": len(sbd_files),
        "Calls_Plan_File": CALLS_PLAN_FILE.name,
        "Easy_SBF_Master_File": EASY_SBF_FILE.name,
        "Name_Master_File": NAME_MASTER_FILE.name,
        "Total_Output_Rows": len(output_df),
    }

    output_path, history_path = save_output(output_df, run_info)

    print()
    print("Output File:")
    print(relpath(output_path))
    print()
    print("History File:")
    print(relpath(history_path))
    print()
    print("=" * 50)
    print("PROCESS COMPLETED")
    print("=" * 50)


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that turns usage errors into friendly command errors."""

    def error(self, message: str) -> None:
        raise CommandError(
            f"{message}\nRun 'python find_easy_sbf.py --help' for usage."
        )


def _date_without_flag(argv: Sequence[str]) -> Optional[str]:
    """Return an argument that looks like a date but was not passed via --date.

    Catches mistakes such as `--08-08-2026` or a bare `08-08-2026`.
    """
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
            "A target date must be passed with the --date flag.\n"
            f"Did you mean:  python find_easy_sbf.py --date {value}"
        )

    parser = _ArgumentParser(
        add_help=False,
        description="Easy SBF Gap Analysis.",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="DD-MM-YYYY",
        help="Process the specified Visit Date (DD-MM-YYYY or YYYY-MM-DD).",
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
    except Stopped:
        print("Process terminated safely.")
        print("=" * 50)
        return 0
    except CommandError as exc:
        logging.error("%s", exc)
        return 1
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""find_opening_easy_sbf.py

Identify the OPENING Easy SBF (Sales Blocking Factors) gap for every customer
found in the Input SBD reports.

This script mirrors find_easy_sbf.py (same normalization, matching and output
workbook structure) but WITHOUT any Calls Plan related logic:

- No Calls Plan load, no target Visit Date targeting, no date filtering.
- Every customer (DSE + Retailer) present in the SBD reports is kept.
- The output carries the RUNNING MONTH instead of a visit date, and the
  filename carries that month too.

Still applied (same business rules as find_easy_sbf.py):
- WS channel rows (channel_description containing "WS") are excluded.
- Only DSEs present in the Name Master are kept.
- No Sale reasons are matched against the Easy SBF master list.

Usage
-----
    python find_opening_easy_sbf.py
    python find_opening_easy_sbf.py --help

Pipeline
--------
1. Read and merge every Input/SBD_report*.CSV.
2. Load updating_files/Easy_SBF_List.xlsx and Name_master.xlsx.
3. Normalize column names, DSE/retailer codes and SBF text.
4. Exclude WS channel rows; keep only DSEs present in the Name Master.
5. Aggregate No Sale reasons per DSE + Retailer customer.
6. Split No Sale reasons, count total gaps and Easy SBF gaps.
7. Write a workbook named after the running month (e.g.
   Opening_Easy_SBF_202608.xlsx) to output/ and a permanent copy in history/,
   never overwriting previous results.
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
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_DIR = BASE_DIR / "history"

NAME_MASTER_FILE = UPDATING_DIR / "Name_master.xlsx"
EASY_SBF_FILE = UPDATING_DIR / "Easy_SBF_List.xlsx"

SBD_PREFIX = "SBD_report"
SBD_SUFFIX = ".csv"

OUTPUT_FILE_PREFIX = "Opening_Easy_SBF"
# WS channel rule: SBD rows whose channel_description contains
# CHANNEL_WS_PATTERN are EXCLUDED; every non-WS channel is kept.
CHANNEL_WS_PATTERN = "WS"
OUTPUT_COLUMNS = [
    "dse_code",
    "retailer_code",
    "month",
    "total_gap",
    "easy_sbf_gap",
    "easy_sbf",
]

BLANK_CODE_VALUES = {"", "NAN", "NONE", "NA", "N/A", "NULL", "<NA>", "NAT"}

COLUMN_ALIASES = {
    "user_code": ("user_code", "usercode"),
    "retailer_code": ("retailer_code", "retailercode"),
    "dse_code": ("dse_code", "dsecode"),
    "channel_description": ("channel_description", "channeldesc"),
    "no_sale": ("no_sale", "nosale"),
    "easy_sbf": ("easy_sbf", "easy_sbf_list", "easy_sbf_master", "easysbf"),
}

COLUMN_FALLBACKS = {
    "dse_code": (("dse",),),
    "channel_description": (("channel",),),
    "no_sale": (("no", "sale"), ("nosale",)),
    "easy_sbf": (("easy", "sbf"), ("sbf",)),
}

HELP_TEXT = """Opening Easy SBF Gap Analysis

Usage:

python find_opening_easy_sbf.py
    Scan every SBD report and produce the Opening Easy SBF gap for each
    customer (DSE + Retailer), stamped with the running month.

No date argument is accepted: the Calls Plan (and its target Visit Date) is not
used. WS channels are excluded and only Name Master DSEs are kept.
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
# Normalization helpers (mirror find_easy_sbf.py)
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
    for standard_key in ("dse_code", "retailer_code", "channel_description", "no_sale"):
        raw = find_column(raw_columns, standard_key)
        if raw is None:
            raise CommandError(
                f"SBD report {path.name} is missing required column "
                f"'{standard_key}'. Available columns: {raw_columns}"
            )
        mapping[standard_key] = raw

    df = _read_csv_any_encoding(path, usecols=list(mapping.values()))
    df = df.rename(columns={raw: std for std, raw in mapping.items()})
    df["dse_code"] = df["dse_code"].map(normalize_code)
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

def filter_non_ws_sbd(sbd: pd.DataFrame) -> pd.DataFrame:
    """Exclude SBD rows whose channel description contains "WS".

    Only non-WS channels (Small / Large / Convenience / Pharmacy / etc.)
    remain; WS / Wholesale rows are dropped.
    """
    non_ws = sbd[~sbd["channel_description"].astype(str).str.contains(CHANNEL_WS_PATTERN, case=False, na=False)].copy()
    return non_ws[non_ws["retailer_code"] != ""]


def filter_valid_dses(sbd: pd.DataFrame, valid_dse_set: set[str]) -> pd.DataFrame:
    """Keep only rows whose DSE is present in the Name Master.

    Rows with blank DSE / retailer codes are dropped too.
    """
    sbd = sbd[(sbd["dse_code"] != "") & (sbd["retailer_code"] != "")]
    return sbd[sbd["dse_code"].isin(valid_dse_set)].copy()


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


def aggregate_customers(sbd: pd.DataFrame) -> pd.DataFrame:
    """Collapse SBD rows to one record per DSE + Retailer customer.

    No Sale reasons of a customer are joined into a single "|"-separated cell
    so the downstream split/matching logic is identical to find_easy_sbf.py.
    """
    if sbd.empty:
        return pd.DataFrame(columns=["dse_code", "retailer_code", "_no_sale"])
    grouped = (
        sbd.groupby(["dse_code", "retailer_code"])["no_sale"]
        .agg(_join_no_sale)
        .reset_index()
        .rename(columns={"no_sale": "_no_sale"})
    )
    return grouped


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
    merged: pd.DataFrame, month: str, easy_sbf_set: set[str]
) -> pd.DataFrame:
    """Build the final output (one row per DSE + Retailer customer).

    Every customer from the filtered SBD reports appears exactly once. The
    output carries the running month instead of a visit date.
    """
    rows: list[dict] = []
    for _, row in merged.iterrows():
        reasons = split_no_sale_reasons(row["_no_sale"])
        easy_gap, easy_text = match_easy_sbf(reasons, easy_sbf_set)
        rows.append(
            {
                "dse_code": row["dse_code"],
                "retailer_code": row["retailer_code"],
                "month": month,
                "total_gap": len(reasons),
                "easy_sbf_gap": easy_gap,
                "easy_sbf": easy_text,
            }
        )
    output = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if not output.empty:
        output = output.sort_values(
            ["dse_code", "retailer_code"], ignore_index=True
        )
    return output


# ---------------------------------------------------------------------------
# Output writers (mirror find_easy_sbf.py)
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
        output_df.to_excel(writer, sheet_name="Opening_Easy_SBF", index=False)
        write_run_info(writer, run_info)


def save_history(path: Path, output_df: pd.DataFrame, run_info: dict) -> None:
    save_workbook(path, output_df, run_info)


def save_output(output_df: pd.DataFrame, run_info: dict) -> tuple[Path, Path]:
    """Write to output/ and history/, preserving previous history files.

    The filename carries the RUNNING MONTH (e.g. Opening_Easy_SBF_202608).
    Re-runs for the same month get a _1, _2 ... suffix so no previous result
    is ever overwritten.
    """
    name = next_available_name(f"{OUTPUT_FILE_PREFIX}_{run_info['File_Month_Stamp']}")
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
    print("       OPENING EASY SBF ANALYSIS")
    print("=" * 50)

    execution_dt = dt.datetime.now()
    timestamp_str = execution_dt.strftime("%Y%m%d_%H%M%S")
    running_month = execution_dt.strftime("%m-%Y")
    file_month_stamp = execution_dt.strftime("%Y%m")

    print()
    print(f"Execution Timestamp : {execution_dt.strftime('%d-%m-%Y %H:%M:%S')}")
    print(f"Running Month       : {running_month}")
    print()
    print("Loading source files...")

    valid_dse_set = load_name_master()
    print("Name Master         : OK")

    easy_sbf_set = load_easy_sbf_master()
    print("Easy SBF Master     : OK")

    sbd, sbd_files = load_sbd_reports()
    print(f"SBD Reports         : {len(sbd_files)} files found")

    print()
    print("-" * 50)
    print("PROCESSING")
    print("-" * 50)

    print(f"SBD Records (raw)   : {len(sbd)}")

    sbd_non_ws = filter_non_ws_sbd(sbd)
    print(f"Non-WS SBD Records  : {len(sbd_non_ws)}")

    sbd_valid = filter_valid_dses(sbd_non_ws, valid_dse_set)
    print(f"Valid DSE Records   : {len(sbd_valid)}")

    merged = aggregate_customers(sbd_valid)
    print(f"Unique Customers    : {len(merged)}")

    print("Processing No Sale...")
    print("Matching Easy SBF...")

    output_df = build_output(merged, running_month, easy_sbf_set)

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
        "Running_Month": running_month,
        "File_Month_Stamp": file_month_stamp,
        "SBD_Files_Processed": len(sbd_files),
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
            f"{message}\nRun 'python find_opening_easy_sbf.py --help' for usage."
        )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = _ArgumentParser(
        add_help=False,
        description="Opening Easy SBF Gap Analysis.",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
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
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

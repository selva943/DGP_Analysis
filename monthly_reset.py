"""monthly_reset.py

Close out the previous month and start a clean one.

Features
--------
* Archives every month-stamped workbook in output/ and history/ whose stamp
  is not the running month (all 2026MM-* files once the clock ticks into
  the next month).
* Resets the dashboard build artifacts (output/site/ and the legacy
  output/dashboard.html) so the next build_dashboard.py run rebuilds from
  the new month only.
* Moves files instead of deleting them. Everything lands in
  history/archive/<YYYY>/<MM>/, which sits outside every glob used by
  build_dashboard.py (it only reads top-level history/<prefix>_*.xlsx and
  history/<YYYY>/*/analytics.parquet), so an archived month can never leak
  back into the dashboard or into achieved_sbf.py's plan matching.
* Dry run by default. Pass --apply to actually move anything.
* Safe to repeat: re-running on the same month finds nothing left to do,
  and archive name collisions get a _1, _2 ... suffix rather than
  overwriting an earlier archive.
* A manifest_<timestamp>.csv is written next to each archived month so the
  move is auditable and reversible by hand.

Run
---
    python monthly_reset.py                    # dry run, shows the plan
    python monthly_reset.py --apply            # archive the stale months
    python monthly_reset.py --apply --month 202609
    python monthly_reset.py --apply --keep-dashboard

The Input folder is never touched: source reports are the operator's to
replace, and deleting them would destroy data the pipeline still needs.
"""

import argparse
import csv
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
HISTORY_DIR = BASE_DIR / "history"

# Archived months live here. Must stay a subfolder of history/ so the
# top-level globs in build_dashboard.py never pick these files up.
ARCHIVE_ROOT = HISTORY_DIR / "archive"

# Dashboard build artifacts to reset so the next build is month-clean.
SITE_DIR = OUTPUT_DIR / "site"
LEGACY_DASHBOARD_NAMES = ("dashboard.html",)

WORKBOOK_EXT = ".xlsx"

# Every monthly artifact is "<prefix>_<YYYYMM>..." or "<prefix>_<YYYYMMDD>..."
# so one leading 6-digit stamp identifies the month; daily files simply carry
# two extra day digits. The trailing lookahead rejects a malformed stamp such
# as "Easy_SBF_Gap_2026091.xlsx" instead of silently reading it as "202609".
MONTHLY_FILE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Easy_SBF_Gap", re.compile(r"^Easy_SBF_Gap_(\d{6}(?:\d{2})?)(?!\d)", re.IGNORECASE)),
    ("Achieved_SBF", re.compile(r"^Achieved_SBF_(\d{6}(?:\d{2})?)(?!\d)", re.IGNORECASE)),
    ("Opening_Easy_SBF", re.compile(r"^Opening_Easy_SBF_(\d{6})(?!\d)", re.IGNORECASE)),
)

HELP_TEXT = """Close out the previous month and start a clean one.

Moves every month-stamped workbook in output/ and history/ whose stamp is
not the running month into history/archive/<YYYY>/<MM>/, then resets the
dashboard build artifacts so the next build_dashboard.py run reflects only
the new month.

Examples:
  python monthly_reset.py                     dry run (default)
  python monthly_reset.py --apply             archive stale months
  python monthly_reset.py --apply --month 202609
  python monthly_reset.py --apply --keep-dashboard
"""


# ---------------------------------------------------------------------------
# Month helpers
# ---------------------------------------------------------------------------
def configure_console() -> None:
    """Make console output encoding-safe on any platform.

    Avoids UnicodeEncodeError when printing symbols like the arrow on
    consoles that use narrow encodings (e.g. cp1252 on Windows).
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def running_month(today: dt.date | None = None) -> str:
    """Return the current month as a YYYYMM stamp."""
    return (today or dt.date.today()).strftime("%Y%m")


def shift_month(month_stamp: str, delta: int) -> str:
    """Return the month_stamp moved by delta months (delta may be negative)."""
    year = int(month_stamp[:4])
    month = int(month_stamp[4:6]) - 1 + delta
    year += month // 12
    month = month % 12 + 1
    return f"{year:04d}{month:02d}"


def previous_month(month_stamp: str) -> str:
    """Return the month before month_stamp (handles the year boundary)."""
    return shift_month(month_stamp, -1)


def format_month(month_stamp: str) -> str:
    """Render a YYYYMM stamp as MM-YYYY for display."""
    return f"{month_stamp[4:6]}-{month_stamp[:4]}"


def extract_month(filename: str) -> str | None:
    """Return the YYYYMM stamp encoded in a monthly workbook filename.

    Returns None for anything that is not a recognized month-stamped
    workbook, so unrelated files are never swept up.
    """
    if Path(filename).suffix.lower() != WORKBOOK_EXT:
        return None
    for _label, pattern in MONTHLY_FILE_RULES:
        match = pattern.match(filename)
        if not match:
            continue
        # Daily files capture YYYYMMDD; keep only the YYYYMM part so every
        # day of a month groups into one archive folder.
        stamp = match.group(1)[:6]
        if 1 <= int(stamp[4:6]) <= 12:
            return stamp
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def scan_dirs() -> tuple[Path, ...]:
    """Return the folders holding month-stamped output.

    Derived from OUTPUT_DIR / HISTORY_DIR on every call so editing either
    config constant above genuinely redirects the scan.
    """
    return (OUTPUT_DIR, HISTORY_DIR)


def collect_month_workbooks(months: set[str] | None) -> list[tuple[Path, str]]:
    """Return (path, month_stamp) for every monthly workbook to be archived.

    months=None archives every stamp except the running month. Passing an
    explicit set restricts the archive to exactly those months.
    """
    found: list[tuple[Path, str]] = []
    for folder in scan_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            stamp = extract_month(path.name)
            if stamp is None:
                continue
            if months is not None and stamp not in months:
                continue
            found.append((path, stamp))
    return found


def collect_dashboard_artifacts() -> list[Path]:
    """Return the dashboard build artifacts that should be reset."""
    artifacts: list[Path] = []
    if SITE_DIR.is_dir():
        artifacts.append(SITE_DIR)
    for name in LEGACY_DASHBOARD_NAMES:
        candidate = OUTPUT_DIR / name
        if candidate.is_file():
            artifacts.append(candidate)
    return artifacts


def resolve_dashboard_month(archived: set[str], current: str) -> str:
    """Pick the month label for the archived dashboard artifacts.

    The dashboard was built from the month just closed, so it is filed
    under the newest month actually archived. When nothing was archived the
    previous month is the sensible label.
    """
    if archived:
        return max(archived)
    return previous_month(current)


# ---------------------------------------------------------------------------
# Archiving
# ---------------------------------------------------------------------------
def archive_dir(month_stamp: str) -> Path:
    """Return history/archive/<YYYY>/<MM> for a month stamp."""
    return ARCHIVE_ROOT / month_stamp[:4] / month_stamp[4:6]


def unique_destination(target: Path) -> Path:
    """Return a free path, appending _1, _2 ... rather than overwriting."""
    if not target.exists():
        return target
    counter = 1
    while True:
        candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def archive_item(src: Path, month_stamp: str) -> Path:
    """Move src into the archive folder for month_stamp and return the new path."""
    dest_dir = archive_dir(month_stamp)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_destination(dest_dir / src.name)
    shutil.move(str(src), str(dest))
    return dest


def write_manifest(month_stamp: str, entries: list[tuple[Path, Path]]) -> Path | None:
    """Record what was moved so the archive can be audited and undone."""
    if not entries:
        return None
    dest_dir = archive_dir(month_stamp)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = unique_destination(dest_dir / f"manifest_{stamp}.csv")
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source", "archived_to", "bytes"])
        for src, dest in entries:
            try:
                size = dest.stat().st_size
            except OSError:
                size = ""
            writer.writerow([str(src), str(dest), size])
    return path


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def dir_size(path: Path) -> int:
    """Total size in bytes of a file, or of a directory tree."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def human_size(num_bytes: int) -> str:
    """Render a byte count in MB for console output."""
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def print_plan(
    workbooks: list[tuple[Path, str]],
    artifacts: list[Path],
    current: str,
    keep_dashboard: bool,
) -> None:
    """Print exactly what the run would archive, grouped by month."""
    by_month: dict[str, list[Path]] = {}
    for path, stamp in workbooks:
        by_month.setdefault(stamp, []).append(path)

    print(f"Running month    : {format_month(current)} ({current})")
    print(f"Archive root     : {ARCHIVE_ROOT}")
    print(f"Workbooks to move: {len(workbooks)}")
    print()

    if not by_month:
        print("  (none - no stale month-stamped workbooks found)")
        print()

    for stamp in sorted(by_month):
        paths = by_month[stamp]
        size = sum(dir_size(p) for p in paths)
        print(f"  {format_month(stamp)} ({stamp}) - {len(paths)} files, {human_size(size)}")
        for path in paths:
            print(f"    {path.relative_to(BASE_DIR)}")
        print(f"    -> {archive_dir(stamp).relative_to(BASE_DIR)}")
        print()

    if keep_dashboard:
        print("Dashboard        : kept (--keep-dashboard)")
        print()
        return

    if artifacts:
        print(f"Dashboard reset  : {len(artifacts)} artifacts")
        for path in artifacts:
            print(f"    {path.relative_to(BASE_DIR)}  ({human_size(dir_size(path))})")
        print()
    else:
        print("Dashboard        : nothing to reset")
        print()


def print_summary(
    moved: int,
    manifests: list[Path],
    current: str,
    applied: bool,
) -> None:
    """Print the closing report."""
    print("-" * 60)
    print()
    print(f"{'Moved' if applied else 'Would move'}   : {moved} item(s)")
    print(f"New month       : {format_month(current)} ({current})")
    if applied:
        print("Status          : Completed successfully.")
    else:
        print("Status          : Dry run. Re-run with --apply to archive.")
    print()
    for path in manifests:
        print(f"Manifest        : {path.relative_to(BASE_DIR)}")
    if applied and manifests:
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="monthly_reset.py",
        description=HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move the files. Without this flag nothing is changed.",
    )
    parser.add_argument(
        "--month",
        metavar="YYYYMM",
        help="Archive only this month instead of every non-running month.",
    )
    parser.add_argument(
        "--keep-dashboard",
        action="store_true",
        help="Leave output/site/ and output/dashboard.html in place.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Plan and (with --apply) execute the month rollover cleanup."""
    configure_console()
    args = parse_args(argv)

    current = running_month()

    only: set[str] | None = None
    if args.month:
        stamp = args.month.strip()
        if not re.fullmatch(r"\d{6}", stamp) or not 1 <= int(stamp[4:6]) <= 12:
            print(f"ERROR: --month must look like YYYYMM, got: {args.month}")
            return 1
        if stamp == current:
            print(
                f"ERROR: {stamp} ({format_month(stamp)}) is the running month. "
                "Refusing to archive the month in progress."
            )
            return 1
        only = {stamp}

    missing = [
        str(d.relative_to(BASE_DIR))
        for d in scan_dirs()
        if not d.is_dir()
    ]
    if missing and only is None:
        print(f"ERROR: expected folder(s) not found: {', '.join(missing)}")
        return 1

    workbooks = collect_month_workbooks(only)
    artifacts = [] if args.keep_dashboard else collect_dashboard_artifacts()

    print_plan(workbooks, artifacts, current, args.keep_dashboard)

    if not args.apply:
        print_summary(0, [], current, applied=False)
        return 0

    if not workbooks and not artifacts:
        print_summary(0, [], current, applied=True)
        return 0

    moved = 0
    manifests: list[Path] = []

    # Group by month so every month lands in its own archive folder and
    # gets its own manifest.
    grouped: dict[str, list[Path]] = {}
    for path, stamp in workbooks:
        grouped.setdefault(stamp, []).append(path)

    if artifacts:
        grouped.setdefault(resolve_dashboard_month(set(grouped), current), []).extend(
            artifacts
        )

    for stamp in sorted(grouped):
        entries: list[tuple[Path, Path]] = []
        for src in grouped[stamp]:
            try:
                dest = archive_item(src, stamp)
            except OSError as exc:
                print(f"ERROR archiving {src.name}: {exc}")
                continue
            entries.append((src, dest))
            moved += 1
        manifest = write_manifest(stamp, entries)
        if manifest is not None:
            manifests.append(manifest)

    print_summary(moved, manifests, current, applied=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""rename.py

Standardize report filenames inside the Input folder.

Features
--------
* Keyword-based report detection (no exact filename matching).
* Automatic CSV extension normalization (e.g. .csv000 -> .CSV).
* Sequential numbering for duplicate report files (never overwrites).
* Renames files in place only: never moves, copies, deletes, or edits
  file contents.

The Input folder is resolved relative to this script so no hard-coded
paths are required. A different folder may be passed as a CLI argument.
"""

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Mapping of canonical report name -> keywords used to detect it.
# Adding a new report type only requires editing this dictionary.
REPORT_MASTER: dict[str, list[str]] = {
    "SBD_report": [
        "sbd",
        "report",
    ],
    "Calls_Plan": [
        "call",
        "calls",
        "plan",
    ],
    "Sales_Report": [
        "sales",
    ],
    "Inventory_Report": [
        "inventory",
        "stock",
    ],
}

# Name used when no report matches the filename.
UNKNOWN_REPORT = "Unknown_Report"

# Standard extension applied to every renamed file.
STANDARD_EXT = ".CSV"

# Matches any CSV variant extension such as .csv, .CSV000, .csv123.
CSV_EXTENSION_PATTERN = re.compile(r"\.csv\d*$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Normalization / matching
# ---------------------------------------------------------------------------
def normalize_name(filename: str) -> str:
    """Reduce a filename to lowercase letters only for fuzzy matching.

    Strips spaces, underscores, hyphens, dots, brackets, special
    characters, digits, and dates, so that "SBD Report_MT HQ202608",
    "SBD_REPORT", "SBD-Report" and "sbdreport" all normalize the same.
    """
    return re.sub(r"[^a-z]+", "", filename.lower())


def match_report(normalized_name: str) -> str | None:
    """Return the report key with the highest keyword match score.

    The score is the number of keywords from a report definition that
    appear in the normalized filename. Returns None when nothing matches.
    """
    best_key: str | None = None
    best_score = 0
    for report_key, keywords in REPORT_MASTER.items():
        score = sum(1 for keyword in keywords if keyword in normalized_name)
        if score > best_score:
            best_key = report_key
            best_score = score
    return best_key


# ---------------------------------------------------------------------------
# Target name resolution
# ---------------------------------------------------------------------------
# All canonical base names: every report key plus the unknown fallback.
CANONICAL_KEYS: list[str] = list(REPORT_MASTER.keys()) + [UNKNOWN_REPORT]

# Matches any already-standard filename, e.g. "SBD_report.CSV", "Sales_Report1.CSV".
STANDARD_NAME_PATTERN = re.compile(
    rf"^(?:{'|'.join(re.escape(key) for key in CANONICAL_KEYS)})\d*\.csv$",
    re.IGNORECASE,
)


def is_standard_name(filename: str) -> bool:
    """True if the file already uses a canonical naming pattern.

    Canonical patterns are "<report>.CSV", "<report>1.CSV", "<report>2.CSV",
    ... Detecting them makes the script idempotent across reruns and avoids
    re-classifying previously renamed files.
    """
    return bool(STANDARD_NAME_PATTERN.match(filename))


def find_available_target(input_dir: Path, report_key: str, current: Path) -> Path:
    """Return the first free target path for a report type.

    Tries "<report>.CSV", then "<report>1.CSV", "<report>2.CSV", ...
    Never returns a path that already exists, so existing files are never
    overwritten.
    """
    counter = 0
    while True:
        suffix = "" if counter == 0 else str(counter)
        candidate = input_dir / f"{report_key}{suffix}{STANDARD_EXT}"
        if not candidate.exists() or candidate == current:
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------
def process_file(path: Path, input_dir: Path) -> tuple[bool, bool]:
    """Rename a single file in place.

    Returns (renamed, is_unknown). The file is left untouched when it is
    already canonical or when an error occurs.
    """
    # Idempotency: skip files already named with any canonical pattern.
    if is_standard_name(path.name):
        print(f"Skipped (already standard): {path.name}\n")
        return False, False

    # Detect the report type from normalized keywords.
    report_key = match_report(normalize_name(path.name))
    if report_key is None:
        report_key = UNKNOWN_REPORT

    target = find_available_target(input_dir, report_key, path)

    # Safety guard: never rename a file onto itself.
    if target == path:
        print(f"Skipped (already standard): {path.name}\n")
        return False, False

    print("Detected:")
    print(path.name)
    print()
    print("Matched:")
    print(report_key)
    print()
    print("Renamed:")
    print(f"{path.name}\n→\n{target.name}")
    print()
    print("-" * 36)
    print()

    path.rename(target)
    return True, report_key == UNKNOWN_REPORT


def scan_and_rename(input_dir: Path) -> None:
    """Process every file inside the input folder."""
    files = sorted((p for p in input_dir.iterdir() if p.is_file()), key=lambda p: p.name)

    files_scanned = 0
    files_renamed = 0
    unknown_files = 0

    print("Scanning Input folder...")
    print()

    for path in files:
        files_scanned += 1
        try:
            renamed, is_unknown = process_file(path, input_dir)
            if renamed:
                files_renamed += 1
            if is_unknown:
                unknown_files += 1
        except OSError as exc:
            print(f"ERROR renaming {path.name}: {exc}\n")

    print("Files scanned:")
    print(files_scanned)
    print()
    print("Files renamed:")
    print(files_renamed)
    print()
    print("Unknown files:")
    print(unknown_files)
    print()
    print("Completed successfully.")


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


def main() -> None:
    """Resolve the Input folder and run the rename pipeline."""
    configure_console()
    if len(sys.argv) > 1:
        input_dir = Path(sys.argv[1]).resolve()
    else:
        input_dir = Path(__file__).resolve().parent / "Input"

    if not input_dir.is_dir():
        print(f"ERROR: Input folder not found: {input_dir}")
        sys.exit(1)

    scan_and_rename(input_dir)


if __name__ == "__main__":
    main()

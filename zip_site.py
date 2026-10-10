"""Zip the built dashboard folder so it can be imported by the live app.

The live app's Admin tab expects an archive of ``output/site`` containing
``index.html`` and the ``dse-data`` folder (manifest.json, core.bin, c/*.bin).

Usage
-----
    python zip_site.py                 # zip output/site -> output/site.zip
    python zip_site.py --out D:\\site.zip
    python zip_site.py --source G:\\path\\to\\site
    python zip_site.py --verify        # only check that a site folder is valid

The archive keeps a top-level ``site/`` folder, which is the layout the
importer expects, so extracting it gives ``site/index.html`` and
``site/dse-data/...``.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = BASE_DIR / "output" / "site"
DEFAULT_OUT = BASE_DIR / "output" / "site.zip"

REQUIRED = ("manifest.json", "core.bin")


def find_data_dir(source: Path) -> Path | None:
    """Locate the dse-data folder inside a site directory."""
    direct = source / "dse-data"
    if direct.is_dir():
        return direct
    for candidate in source.rglob("dse-data"):
        if candidate.is_dir():
            return candidate
    return None


def validate(source: Path) -> Path:
    """Check the folder looks like a built dashboard; return its dse-data path."""
    if not source.is_dir():
        raise SystemExit(f"ERROR: source folder not found: {source}")

    data_dir = find_data_dir(source)
    if data_dir is None:
        raise SystemExit(f"ERROR: no dse-data folder inside {source}")

    missing = [name for name in REQUIRED if not (data_dir / name).is_file()]
    if missing:
        raise SystemExit(
            f"ERROR: {data_dir} is missing {', '.join(missing)}.\n"
            "Run build_dashboard.py first to generate the site."
        )

    chunks = list((data_dir / "c").glob("*.bin")) if (data_dir / "c").is_dir() else []
    return data_dir, len(chunks)  # type: ignore[return-value]


def build_zip(source: Path, out: Path) -> Path:
    data_dir, chunk_count = validate(source)  # type: ignore[misc]

    if out.exists():
        out.unlink()

    written = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            # Keep the "site/..." prefix so the archive unpacks to site/...
            arcname = Path("site") / path.relative_to(source)
            zf.write(path, arcname.as_posix())
            written += 1

    if written == 0:
        out.unlink(missing_ok=True)
        raise SystemExit(f"ERROR: nothing to zip in {source}")

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"source   : {source}")
    print(f"chunks   : {chunk_count} customer files in {data_dir.name}/")
    print(f"files    : {written}")
    print(f"archive  : {out}")
    print(f"size     : {size_mb:.2f} MB")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zip the built dashboard site folder.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help="site folder to zip (default: output/site)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="archive to write (default: output/site.zip)")
    parser.add_argument("--verify", action="store_true",
                        help="only validate the folder, do not write an archive")
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    out = Path(args.out).resolve()

    try:
        if args.verify:
            data_dir, chunk_count = validate(source)  # type: ignore[misc]
            print(f"OK: {data_dir} ({chunk_count} chunks)")
            return 0
        build_zip(source, out)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\nUpload this archive in the app: Admin -> Import zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Publish the built dashboard bundle to the database.

Run this on the machine that holds the DGP project files whenever a new site has
been built. It is the supported way to update the deployed app when the web
server cannot accept long uploads (Vercel's 60s function limit, for example).

    python publish_site.py                  # read output/site from disk
    python publish_site.py --zip site.zip   # read a zipped bundle
    python publish_site.py --dry-run        # decrypt and report, write nothing

Everything it does matches Admin -> Import in the UI: decrypt the bundle,
replace the active dataset version, and point viewers at the new data.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LIVEAPP_DIR = BASE_DIR / "liveapp"
if str(LIVEAPP_DIR) not in sys.path:
    sys.path.insert(0, str(LIVEAPP_DIR))


def log(message: str = "") -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the site bundle to the database.")
    parser.add_argument("--zip", help="path to a zipped output/site bundle")
    parser.add_argument("--source", help="site folder (default: output/site)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decrypt and report without writing to the database")
    parser.add_argument("--by", default="publish_site.py",
                        help="recorded as the author of the new dataset version")
    args = parser.parse_args(argv)

    try:
        from app.services import site_bundle
    except Exception as exc:
        log(f"ERROR: cannot import the app package: {exc}")
        log("Run this from the DGP_analysis folder, with dependencies installed:")
        log("  pip install -r liveapp/requirements.txt")
        return 1

    try:
        source = Path(args.source) if args.source else site_bundle.default_site_dir()
        if args.zip:
            log(f"Reading archive {args.zip}")
            payload = site_bundle.import_from_zip(Path(args.zip).read_bytes(), log)
        else:
            log(f"Reading bundle from {source}")
            payload = site_bundle.import_from_disk(source, log)
    except site_bundle.SiteBundleError as exc:
        log(f"ERROR: {exc}")
        return 2
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 2

    meta = payload.get("meta", {}) or {}
    dates = payload.get("dates", {}) or {}
    log("")
    log(f"  generated : {meta.get('generated', '?')}")
    log(f"  DSEs      : {meta.get('n_dse', '?')}")
    log(f"  customers : {meta.get('n_customers', len(payload.get('customers') or {}))}")
    log(f"  plan dates: {', '.join(dates.get('plan') or [])}")

    if args.dry_run:
        log("\nDry run - nothing was written to the database.")
        return 0

    try:
        from app.database import SessionLocal, init_db
        from app.services import store

        init_db()
    except Exception as exc:
        log(f"ERROR: cannot reach the database: {exc}")
        log("Check DGP_DATABASE_URL in liveapp/.env")
        return 3

    started = time.time()
    db = SessionLocal()
    try:
        version_id = store.save_payload(db, payload, created_by=args.by)
    except Exception as exc:
        log(f"ERROR: publishing failed: {exc}")
        return 3
    finally:
        db.close()

    log("")
    log(f"Published dataset version #{version_id} in {time.time() - started:.1f}s")
    log("Viewers now see this data on their next page load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
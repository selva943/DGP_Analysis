"""Dev entrypoint for the DGP Live Dashboard.

Run from anywhere:

    python liveapp/main.py
    python liveapp/main.py --port 8080 --no-reload

or directly with uvicorn (cwd must be liveapp/):

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

LIVEAPP_DIR = Path(__file__).resolve().parent
if str(LIVEAPP_DIR) not in sys.path:
    sys.path.insert(0, str(LIVEAPP_DIR))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DGP live dashboard API.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", default=True)
    parser.add_argument("--no-reload", dest="reload", action="store_false")
    return parser.parse_args()


if __name__ == "__main__":
    import uvicorn

    args = parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(LIVEAPP_DIR)],
        log_level="info",
    )
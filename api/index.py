"""Vercel serverless entrypoint.

Vercel imports this module and serves the exported ASGI ``app``. The whole
FastAPI application lives in ``liveapp/app``; this file only bridges to it.

The read paths (/api/core, /api/dse/..., /api/login, /api/activity/open) are
sub-second, so they work well in a serverless function. The bundle import does
not: writing tens of thousands of rows takes far longer than the function
limit, which is why DGP_IMPORT_ENABLED is 0 in production and publishing is
done from the source machine with publish_site.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVEAPP = ROOT / "liveapp"

# Vercel bundles a function by statically tracing imports. The app lives in
# liveapp/app/, which is only reachable through the sys.path entry set up
# here at runtime - a tracer cannot follow that, so the package could be left
# out of the bundle. Importing it explicitly below makes the dependency
# visible to the tracer.
for p in (str(LIVEAPP), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.main import app  # noqa: E402

__all__ = ["app"]
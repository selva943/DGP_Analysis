"""Launcher for the standalone data-processing runner.

    python run_process.py <job_id>
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIVEAPP = Path(__file__).resolve().parent
if str(_LIVEAPP) not in sys.path:
    sys.path.insert(0, str(_LIVEAPP))

from app.services.process_runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

"""Standalone data-processing runner (spawned by the admin endpoint).

Run as::

    python run_process.py <job_id>

It refreshes the source ETL outputs, ingests them into the database, rebuilds
the dashboard payload and publishes it as a new active dataset version. Running
in a separate process isolates the heavy pandas work from the API and keeps the
server responsive while processing.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import traceback

from ..config import settings
from ..database import SessionLocal, init_db
from ..models import ProcessJob
from . import ingest, store
from .builder import build_payload

# Filesystem ETL steps, in order. Each is best-effort: a non-zero exit is
# logged but does not abort the run, because later steps can still succeed.
SOURCE_STEPS: list[tuple[str, str]] = [
    ("rename", "Rename.py"),
    ("plan", "find_easy_sbf.py"),
    ("achieved", "achieved_sbf.py"),
    ("opening", "find_opening_easy_sbf.py"),
]


class _LogBuffer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str = "") -> None:
        line = str(message)
        self.lines.append(line)
        print(line, flush=True)

    def text(self) -> str:
        return "\n".join(self.lines)


def _run_source_scripts(log: _LogBuffer, plan_date: str | None = None, achieved_date: str | None = None) -> None:
    if settings.skip_source_scripts:
        log("Source ETL scripts skipped (DGP_SKIP_SOURCE_SCRIPTS=1).")
        return
    for name, script in SOURCE_STEPS:
        path = settings.project_root / script
        if not path.is_file():
            log(f"[{name}] skipped: {script} not found")
            continue
        extra: list[str] = []
        if name == "plan" and plan_date:
            extra = ["--date", plan_date]
        elif name == "achieved" and achieved_date:
            extra = ["--date", achieved_date]
        log(f"[{name}] running {script} {' '.join(extra)} ..." if extra else f"[{name}] running {script} ...")
        try:
            proc = subprocess.run(
                [sys.executable, str(path), *extra],
                cwd=str(settings.project_root),
                capture_output=True,
                text=True,
                timeout=settings.etl_step_timeout,
            )
        except subprocess.TimeoutExpired:
            log(f"[{name}] TIMEOUT after {settings.etl_step_timeout}s")
            continue
        except Exception as exc:  # pragma: no cover - defensive
            log(f"[{name}] failed to launch: {exc}")
            continue
        tail = (proc.stdout or "").strip().splitlines()[-20:]
        for line in tail:
            log(f"[{name}] {line}")
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()[-5:]
            for line in err:
                log(f"[{name}] stderr: {line}")
            log(f"[{name}] exited with code {proc.returncode} (continuing)")


def run_pipeline(job_id: int) -> int:
    log = _LogBuffer()
    init_db()
    db = SessionLocal()
    try:
        job = db.get(ProcessJob, job_id)
        if job is None:
            print(f"Job {job_id} not found", file=sys.stderr)
            return 2
        job.status = "running"
        job.log = ""
        db.commit()

        log(f"Process job #{job_id} started at {dt.datetime.now():%d-%m-%Y %H:%M:%S}")
        log(f"Project root: {settings.project_root}")
        if job.report_type:
            log(f"Report type: {job.report_type}")
        if job.plan_date:
            log(f"Plan date: {job.plan_date}")
        if job.achieved_date:
            log(f"Achieved date: {job.achieved_date}")

        _run_source_scripts(log, plan_date=job.plan_date, achieved_date=job.achieved_date)

        ingest.ingest_all(db, log=log)
        payload = build_payload(db, log=log)
        version_id = store.save_payload(db, payload, created_by=job.triggered_by)

        job.version_id = version_id
        job.status = "success"
        job.finished_at = dt.datetime.utcnow()
        job.log = log.text()
        db.commit()
        log(f"Published dataset version #{version_id}.")
        return 0
    except Exception as exc:  # pragma: no cover - surfaced via job status
        traceback.print_exc()
        log(f"FAILED: {exc}")
        try:
            job = db.get(ProcessJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = dt.datetime.utcnow()
                job.log = log.text()
                db.commit()
        except Exception:
            pass
        return 1
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python run_process.py <job_id>", file=sys.stderr)
        return 2
    try:
        job_id = int(argv[0])
    except ValueError:
        print(f"invalid job id: {argv[0]}", file=sys.stderr)
        return 2
    return run_pipeline(job_id)


if __name__ == "__main__":
    raise SystemExit(main())

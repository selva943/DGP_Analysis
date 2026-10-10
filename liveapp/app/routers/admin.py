"""Administrator-only routes: upload reports, trigger and monitor processing."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import LIVEAPP_DIR, settings
from ..database import get_db
from ..deps import require_admin
from ..models import ProcessJob, User
from ..services import activity, site_bundle, store


class _LogBuffer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str = "") -> None:
        self.lines.append(str(message))

    def text(self) -> str:
        return "\n".join(self.lines)

router = APIRouter(prefix="/api/admin", tags=["admin"])

RUN_PROCESS = LIVEAPP_DIR / "run_process.py"


def _job_out(job: ProcessJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "triggered_by": job.triggered_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "version_id": job.version_id,
        "error": job.error or "",
        "log": job.log or "",
        "report_type": job.report_type or "",
        "plan_date": job.plan_date,
        "achieved_date": job.achieved_date,
    }


def _validate_report_date(value: str | None, field: str) -> str | None:
    """Accept ``YYYY-MM-DD`` or ``DD-MM-YYYY``; the scripts parse both."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            datetime.strptime(value, fmt)
            return value
        except ValueError:
            continue
    raise HTTPException(
        status_code=400, detail=f"{field} must be DD-MM-YYYY or YYYY-MM-DD, got '{value}'"
    )


def _spawn(job: ProcessJob, db: Session) -> None:
    try:
        subprocess.Popen(
            [sys.executable, str(RUN_PROCESS), str(job.id)],
            cwd=str(LIVEAPP_DIR),
        )
    except Exception as exc:  # pragma: no cover - defensive
        job.status = "failed"
        job.error = f"Could not start processor: {exc}"
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


def _running_job(db: Session) -> ProcessJob | None:
    """Return the live job, clearing one that was abandoned when a run crashed.

    A job is only "running" if it is still young. Without this, a killed
    process (or a server restart mid-import) leaves a permanent 'running' row
    that blocks every future upload with a 409.
    """
    job = (
        db.query(ProcessJob)
        .filter(ProcessJob.status == "running")
        .order_by(ProcessJob.id.desc())
        .first()
    )
    if job is None:
        return None

    started = job.started_at
    if started is not None:
        if started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        age = dt.datetime.utcnow() - started
        if age > dt.timedelta(seconds=settings.etl_step_timeout):
            job.status = "failed"
            job.error = "Abandoned: no completion was recorded before the timeout."
            job.finished_at = dt.datetime.utcnow()
            db.commit()
            return None
    return job


def _require_idle(db: Session) -> None:
    running = _running_job(db)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A processing job is already running (job #{running.id}).",
        )


@router.post("/import-site")
async def import_site(
    file: UploadFile | None = File(None),
    from_disk: bool = Form(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Load a built dashboard bundle (output/site) into the database.

    Upload a zip of output/site, or pass from_disk=true to read the folder that
    the ETL scripts write to on this server. The bundle is decrypted and stored
    as the active dataset version, which is what viewers read.

    This never runs the ETL scripts and never touches Input/.
    """
    _require_idle(db)

    if not settings.import_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Browser import is disabled on this deployment. Run "
                "'python publish_site.py' on the machine that holds output/site."
            ),
        )

    if not from_disk and file is None:
        raise HTTPException(
            status_code=400,
            detail="Attach a zip of output/site, or send from_disk=true.",
        )

    log = _LogBuffer()
    job = ProcessJob(
        status="running",
        triggered_by=admin.username,
        report_type="site-import",
        log="",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        log(f"Import started at {dt.datetime.now():%d-%m-%Y %H:%M:%S}")
        if from_disk:
            site_dir = site_bundle.default_site_dir()
            log(f"Reading bundle from {site_dir}")
            payload = site_bundle.import_from_disk(site_dir, log)
        else:
            raw = await file.read()
            log(f"Uploaded archive: {len(raw):,} bytes")
            payload = site_bundle.import_from_zip(raw, log)

        version_id = store.save_payload(db, payload, created_by=admin.username)
        job.version_id = version_id
        job.status = "success"
        job.finished_at = dt.datetime.utcnow()
        job.log = log.text()
        db.commit()
    except site_bundle.SiteBundleError as exc:
        job.status = "failed"
        job.error = str(exc)
        job.finished_at = dt.datetime.utcnow()
        job.log = log.text()
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # pragma: no cover - surfaced via job status
        job.status = "failed"
        job.error = str(exc)
        job.finished_at = dt.datetime.utcnow()
        job.log = log.text()
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))

    meta = payload.get("meta", {})
    return {
        "version_id": version_id,
        "job_id": job.id,
        "status": "success",
        "n_dse": meta.get("n_dse"),
        "n_customers": meta.get("n_customers"),
        "plan_dates": (payload.get("dates") or {}).get("plan"),
    }


@router.post("/process")
def start_process(
    plan_date: str | None = Form(None),
    achieved_date: str | None = Form(None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    _require_idle(db)

    plan_date = _validate_report_date(plan_date, "plan_date")
    achieved_date = _validate_report_date(achieved_date, "achieved_date")

    job = ProcessJob(
        status="running",
        triggered_by=admin.username,
        report_type="manual",
        plan_date=plan_date,
        achieved_date=achieved_date,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _spawn(job, db)

    return {"job_id": job.id, "status": "running"}


@router.get("/process/{job_id}")
def process_status(
    job_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    job = db.get(ProcessJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_out(job)


@router.get("/jobs")
def list_jobs(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[dict]:
    jobs = (
        db.query(ProcessJob)
        .order_by(ProcessJob.id.desc())
        .limit(limit)
        .all()
    )
    return [_job_out(j) for j in jobs]


@router.get("/users")
def list_users(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> list[dict]:
    users = db.query(User).order_by(User.role.desc(), User.username).all()
    return [
        {
            "username": u.username,
            "role": u.role,
            "display_name": u.display_name,
            "is_active": u.is_active,
        }
        for u in users
    ]


@router.get("/activity")
def viewer_activity(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Per day/per user open counts for the Viewer Activity monitor."""
    today = dt.date.today()
    try:
        start = activity._parse_date(date_from, today)
        end = activity._parse_date(date_to, start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rows = activity.query_rows(db, start, end)
    return {"summary": activity.summarize(rows, start, end), "rows": rows}


@router.get("/activity/export")
def export_activity(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Download the activity report as CSV or XLSX."""
    today = dt.date.today()
    try:
        start = activity._parse_date(date_from, today)
        end = activity._parse_date(date_to, start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    rows = activity.query_rows(db, start, end)
    summary = activity.summarize(rows, start, end)
    name = activity.filename(format, start, end)

    if format == "xlsx":
        return Response(
            content=activity.to_xlsx(rows, summary),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return Response(
        content=activity.to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/status")
def admin_status(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> dict:
    version = store.active_version(db)
    running = db.query(ProcessJob).filter(ProcessJob.status == "running").first()
    return {
        "has_dataset": version is not None,
        "import_enabled": settings.import_enabled,
        "active_version": (
            {
                "id": version.id,
                "created_at": version.created_at.isoformat() if version.created_at else None,
                "created_by": version.created_by,
            }
            if version is not None
            else None
        ),
        "processing": running is not None,
        "running_job_id": running.id if running is not None else None,
        "user_count": db.query(User).count(),
    }

"""Administrator-only routes: upload reports, trigger and monitor processing."""

from __future__ import annotations

import datetime as dt
import secrets
import subprocess
import sys

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import LIVEAPP_DIR, settings
from ..database import get_db
from ..deps import require_admin
from ..models import ProcessJob, UploadSession, User
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


def _require_local_processing() -> None:
    """Refuse up-front when this deployment cannot spawn the ETL runner.

    Checked before any job row is written: once a row exists with status
    'running' it blocks every later upload with a 409 until it ages out, so a
    rejected request must never leave one behind.
    """
    if not settings.local_processing_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Server-side processing is disabled on this deployment. Run the "
                "ETL locally and publish with 'python publish_site.py'."
            ),
        )


def _spawn(job: ProcessJob, db: Session) -> None:
    """Start the out-of-process ETL runner."""
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


# ---------------------------------------------------------------------------
# Chunked bundle upload
#
# A serverless request body is capped at 4.5 MB and output/site.zip is already
# ~4.1 MB, so a single POST has almost no headroom. The browser slices the zip
# into CHUNK_BYTES pieces and posts each one; the parts are staged in the
# database because a serverless instance cannot be relied on to stay alive
# between requests.
# ---------------------------------------------------------------------------

CHUNK_BYTES = 3 * 1024 * 1024  # comfortably under the 4.5 MB body cap
_MAX_CHUNK_BYTES = 4 * 1024 * 1024
_UPLOAD_TTL_HOURS = 24


@router.post("/import-site/chunked/init")
def chunked_init(
    filename: str = Form("site.zip"),
    total_chunks: int = Form(0),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Open a chunked upload session and report how to slice the file."""
    _require_idle(db)
    if total_chunks < 1 or total_chunks > 512:
        raise HTTPException(
            status_code=400,
            detail=f"total_chunks must be 1-512, got {total_chunks}.",
        )
    upload_id = secrets.token_hex(16)
    db.add(
        UploadSession(
            upload_id=upload_id,
            chunk_index=-1,
            data=b"",
            filename=filename[:255],
            total_chunks=total_chunks,
            created_by=admin.username,
        )
    )
    db.commit()
    return {
        "upload_id": upload_id,
        "chunk_bytes": CHUNK_BYTES,
        "total_chunks": total_chunks,
    }


@router.post("/import-site/chunked/part")
async def chunked_part(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Store one slice. Safe to retry: an existing index is overwritten."""
    session = _upload_session(db, upload_id)
    raw = await chunk.read()
    if len(raw) > _MAX_CHUNK_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"chunk is {len(raw):,} bytes; the limit is {_MAX_CHUNK_BYTES:,}.",
        )
    if not (0 <= chunk_index < max(session.total_chunks, 1)):
        raise HTTPException(
            status_code=400,
            detail=f"chunk_index {chunk_index} is outside this session.",
        )

    existing = (
        db.query(UploadSession)
        .filter(
            UploadSession.upload_id == upload_id,
            UploadSession.chunk_index == chunk_index,
        )
        .first()
    )
    if existing is not None:
        existing.data = raw
    else:
        db.add(
            UploadSession(
                upload_id=upload_id,
                chunk_index=chunk_index,
                data=raw,
                filename=session.filename,
                total_chunks=session.total_chunks,
                created_by=admin.username,
            )
        )
    db.commit()
    return {"received": chunk_index, "bytes": len(raw)}


def _upload_session(db: Session, upload_id: str) -> UploadSession:
    session = (
        db.query(UploadSession)
        .filter(UploadSession.upload_id == upload_id, UploadSession.chunk_index == -1)
        .first()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id.")
    return session


def _assemble(db: Session, upload_id: str) -> bytes:
    """Join the stored slices in order, or explain what is still missing."""
    session = _upload_session(db, upload_id)
    parts = (
        db.query(UploadSession)
        .filter(
            UploadSession.upload_id == upload_id,
            UploadSession.chunk_index >= 0,
        )
        .order_by(UploadSession.chunk_index)
        .all()
    )
    received = {p.chunk_index for p in parts}
    expected = set(range(session.total_chunks))
    missing = sorted(expected - received)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Upload incomplete: missing chunk(s) {missing[:20]}.",
        )
    return b"".join(bytes(p.data) for p in parts)


def _drop_upload(db: Session, upload_id: str) -> None:
    db.query(UploadSession).filter(UploadSession.upload_id == upload_id).delete()
    db.commit()


def _publish(db: Session, admin: User, archive: bytes, log: "_LogBuffer") -> dict:
    """Run the shared import + publish path for a bundle held in memory."""
    version_id = None
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
        log(f"Archive: {len(archive):,} bytes")
        payload = site_bundle.import_from_zip(archive, log)
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.finished_at = dt.datetime.utcnow()
        job.log = log.text()
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    meta = payload.get("meta", {})
    return {
        "version_id": version_id,
        "job_id": job.id,
        "status": "success",
        "n_dse": meta.get("n_dse"),
        "n_customers": meta.get("n_customers"),
        "plan_dates": (payload.get("dates") or {}).get("plan"),
    }


@router.post("/import-site/chunked/commit")
def chunked_commit(
    upload_id: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Assemble the uploaded slices and publish them as the active dataset."""
    if not settings.import_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Browser import is disabled on this deployment. Run "
                "'python publish_site.py' on the machine that holds output/site."
            ),
        )
    _require_idle(db)
    try:
        archive = _assemble(db, upload_id)
        result = _publish(db, admin, archive, _LogBuffer())
    finally:
        _drop_upload(db, upload_id)
    return result


@router.post("/import-site")
async def import_site(
    file: UploadFile | None = File(None),
    from_disk: bool = Form(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    """Load a built dashboard bundle (output/site) into the database.

    Single-request form. Kept for local use and for bundles that fit under the
    platform's request-body cap; anything larger must use the chunked endpoints,
    which is what the admin panel does.
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

    if from_disk:
        site_dir = site_bundle.default_site_dir()
        archive = None
        log = _LogBuffer()
        log(f"Reading bundle from {site_dir}")
        payload = site_bundle.import_from_disk(site_dir, log)
        job = _record_import(db, admin, log, payload)
        meta = payload.get("meta", {})
        return {
            "version_id": job.version_id,
            "job_id": job.id,
            "status": "success",
            "n_dse": meta.get("n_dse"),
            "n_customers": meta.get("n_customers"),
            "plan_dates": (payload.get("dates") or {}).get("plan"),
        }

    raw = await file.read()
    return _publish(db, admin, raw, _LogBuffer())


def _record_import(db: Session, admin: User, log: "_LogBuffer", payload: dict) -> ProcessJob:
    """Persist a dataset built straight from disk (no archive in memory)."""
    version_id = store.save_payload(db, payload, created_by=admin.username)
    job = ProcessJob(
        status="success",
        triggered_by=admin.username,
        report_type="site-import",
        version_id=version_id,
        finished_at=dt.datetime.utcnow(),
        log=log.text(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.post("/process")
def start_process(
    plan_date: str | None = Form(None),
    achieved_date: str | None = Form(None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    _require_local_processing()
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
        "local_processing_enabled": settings.local_processing_enabled,
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

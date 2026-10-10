"""Viewer activity tracking and reporting.

One row is written per dashboard open. The admin report aggregates those rows
per (day, user) to show how often and when each person last looked at the
dashboard, and can export the same rows as CSV or XLSX.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ViewerActivity

DATE_FMT = "%Y-%m-%d"
STAMP_FMT = "%d-%m-%Y, %I:%M:%S %p"
EXPORT_COLUMNS = ("date", "name", "code", "role", "opens", "first_open", "last_open")

# Timestamps are stored in the server's local time on purpose. The app runs on
# the same machine as the people it monitors, so the report must read as their
# wall clock ("opened at 1:30 PM"), and grouping by day must match their day
# boundary rather than UTC's.


def record_open(
    db: Session,
    username: str,
    role: str,
    display_name: str,
    ip: str = "",
    dataset_version: int | None = None,
) -> ViewerActivity:
    """Store one dashboard open (server-local timestamp)."""
    row = ViewerActivity(
        username=username,
        role=role or "viewer",
        display_name=display_name or username,
        opened_at=dt.datetime.now(),
        ip=(ip or "")[:64],
        dataset_version=dataset_version,
    )
    db.add(row)
    db.commit()
    return row


def _parse_date(value: str | None, fallback: dt.date) -> dt.date:
    if not value:
        return fallback
    text = str(value).strip()
    for fmt in (DATE_FMT, "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}. Use YYYY-MM-DD or DD-MM-YYYY.")


def day_bounds(start: dt.date, end: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Half-open local-time bounds covering the whole requested day range."""
    return dt.datetime.combine(start, dt.time.min), dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min)


def query_rows(db: Session, start: dt.date, end: dt.date) -> list[dict]:
    """Aggregate opens per (day, user), newest day first."""
    if start > end:
        start, end = end, start

    lo, hi = day_bounds(start, end)
    stmt = (
        select(
            func.date(ViewerActivity.opened_at).label("day"),
            ViewerActivity.username,
            ViewerActivity.role,
            ViewerActivity.display_name,
            func.count(ViewerActivity.id).label("opens"),
            func.min(ViewerActivity.opened_at).label("first_open"),
            func.max(ViewerActivity.opened_at).label("last_open"),
        )
        .where(ViewerActivity.opened_at >= lo, ViewerActivity.opened_at < hi)
        .group_by(
            "day",
            ViewerActivity.username,
            ViewerActivity.role,
            ViewerActivity.display_name,
        )
        .order_by(func.date(ViewerActivity.opened_at).desc(), ViewerActivity.username)
    )

    out: list[dict] = []
    for r in db.execute(stmt):
        out.append(
            {
                "date": str(r.day),
                "name": r.display_name or r.username,
                "code": r.username,
                "role": r.role,
                "opens": int(r.opens),
                "first_open": r.first_open.strftime(STAMP_FMT) if r.first_open else "",
                "last_open": r.last_open.strftime(STAMP_FMT) if r.last_open else "",
            }
        )
    return out


def summarize(rows: Iterable[dict], start: dt.date, end: dt.date) -> dict:
    rows = list(rows)
    return {
        "from": start.strftime(DATE_FMT),
        "to": end.strftime(DATE_FMT),
        "days": (end - start).days + 1,
        "users": len({r["code"] for r in rows}),
        "opens": sum(r["opens"] for r in rows),
        "rows": len(rows),
    }


def to_csv(rows: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([r[c] for c in EXPORT_COLUMNS])
    return buf.getvalue()


def to_xlsx(rows: list[dict], summary: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Viewer Activity"

    ws["A1"] = "Viewer Activity Report"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"From: {summary['from']}    To: {summary['to']}"
    ws["A3"] = (
        f"Days: {summary['days']}    Users: {summary['users']}"
        f"    Opens: {summary['opens']}"
    )

    header_row = 5
    for col, name in enumerate(EXPORT_COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col, value=name.replace("_", " ").title())
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(horizontal="center")

    for i, r in enumerate(rows, start=header_row + 1):
        for col, name in enumerate(EXPORT_COLUMNS, start=1):
            ws.cell(row=i, column=col, value=r[name])

    widths = [14, 26, 14, 10, 9, 24, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def filename(fmt: str, start: dt.date, end: dt.date) -> str:
    span = start.strftime(DATE_FMT) if start == end else f"{start.strftime(DATE_FMT)}_to_{end.strftime(DATE_FMT)}"
    return f"viewer_activity_{span}.{fmt}"
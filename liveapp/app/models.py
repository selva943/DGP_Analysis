"""SQLAlchemy ORM models for the live DGP dashboard."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )


class NameMaster(Base):
    __tablename__ = "name_master"

    dse_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    dse_name: Mapped[str] = mapped_column(String(255), default="")
    cluster: Mapped[str] = mapped_column(String(255), default="")
    asm: Mapped[str] = mapped_column(String(255), default="")
    branch: Mapped[str] = mapped_column(String(255), default="")
    channel: Mapped[str] = mapped_column(String(128), default="")


class Retailer(Base):
    __tablename__ = "retailer"

    retailer_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_name: Mapped[str] = mapped_column(String(255), default="")
    payment_mode: Mapped[str] = mapped_column(String(128), default="")


class EasySbfMaster(Base):
    __tablename__ = "easy_sbf_master"

    reason_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    reason_text: Mapped[str] = mapped_column(String(255), default="")


class EasySbfGap(Base):
    """PLAN facts: gap per DSE + Retailer + Visit Date."""

    __tablename__ = "easy_sbf_gap"

    dse_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    visit_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    total_gap: Mapped[int] = mapped_column(Integer, default=0)
    easy_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    easy_sbf: Mapped[list] = mapped_column(JSON, default=list)


class AchievedSbf(Base):
    """ACHIEVED facts: plan vs current SBD per DSE + Retailer + Visit Date."""

    __tablename__ = "achieved_sbf"

    dse_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    visit_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    historical_easy_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    achieved_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    achieved_sbf: Mapped[list] = mapped_column(JSON, default=list)
    still_open_easy_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    still_open_easy_sbf: Mapped[list] = mapped_column(JSON, default=list)
    new_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    new_sbf: Mapped[list] = mapped_column(JSON, default=list)
    comparison_status: Mapped[str] = mapped_column(String(64), default="")


class OpeningEasySbf(Base):
    """Monthly locked opening baseline per DSE + Retailer."""

    __tablename__ = "opening_easy_sbf"

    dse_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(16), primary_key=True)
    total_gap: Mapped[int] = mapped_column(Integer, default=0)
    easy_sbf_gap: Mapped[int] = mapped_column(Integer, default=0)
    easy_sbf: Mapped[list] = mapped_column(JSON, default=list)


class AgedReceivable(Base):
    __tablename__ = "aged_receivable"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dse_code: Mapped[str] = mapped_column(String(64), index=True)
    retailer_code: Mapped[str] = mapped_column(String(64), index=True)
    aging_days: Mapped[int] = mapped_column(Integer, default=0)
    ar_status: Mapped[str] = mapped_column(String(128), default="")
    balance_amount: Mapped[float] = mapped_column(Float, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, default=0.0)


class CustomerSnapshot(Base):
    __tablename__ = "customer_snapshot"

    business_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    dse_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    retailer_name: Mapped[str] = mapped_column(String(255), default="")
    gap_count: Mapped[int] = mapped_column(Integer, default=0)
    easy_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_count: Mapped[int] = mapped_column(Integer, default=0)
    with_sale_count: Mapped[int] = mapped_column(Integer, default=0)
    easy_sold_count: Mapped[int] = mapped_column(Integer, default=0)
    planned_today: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested_today: Mapped[bool] = mapped_column(Boolean, default=False)


class ViewerActivity(Base):
    """One row per dashboard open, used by the admin Viewer Activity monitor."""

    __tablename__ = "viewer_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    display_name: Mapped[str] = mapped_column(String(128), default="")
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    dataset_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ProcessJob(Base):
    __tablename__ = "process_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    triggered_by: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    log: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_type: Mapped[str] = mapped_column(String(16), default="")
    plan_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    achieved_date: Mapped[str | None] = mapped_column(String(16), nullable=True)


class UploadSession(Base):
    """A chunked upload of a dashboard bundle, assembled on commit.

    Vercel caps a request body at 4.5 MB and the built ``output/site.zip`` is
    already about 4.1 MB, so a single multipart POST has almost no headroom and
    breaks as soon as a month of data is added.

    Parts are staged in the database rather than in ``/tmp`` on purpose. A
    serverless instance is not guaranteed to be the same one for the next
    request, so local disk cannot be relied on to hold half an upload; the
    database is shared by every instance.
    """

    __tablename__ = "upload_session"
    __table_args__ = (UniqueConstraint("upload_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    filename: Mapped[str] = mapped_column(String(255), default="")
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    core: Mapped["DashboardCore"] = relationship(
        back_populates="version", uselist=False, cascade="all, delete-orphan"
    )
    customers: Mapped[list["DashboardCustomer"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class DashboardCore(Base):
    """meta / tree / dse_customers / opening / dates / levels for one version."""

    __tablename__ = "dashboard_core"

    version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict] = mapped_column(JSON, default=dict)

    version: Mapped[DatasetVersion] = relationship(back_populates="core")


class DashboardCustomer(Base):
    __tablename__ = "dashboard_customer"
    __table_args__ = (UniqueConstraint("version_id", "dse_code", "retailer_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_version.id", ondelete="CASCADE"), index=True
    )
    dse_code: Mapped[str] = mapped_column(String(64), index=True)
    retailer_code: Mapped[str] = mapped_column(String(64), index=True)
    record: Mapped[dict] = mapped_column(JSON, default=dict)

    version: Mapped[DatasetVersion] = relationship(back_populates="customers")

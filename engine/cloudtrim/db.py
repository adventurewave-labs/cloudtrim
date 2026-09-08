"""SQLite persistence for scans, findings, remediation actions, pipeline state."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    desc,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

engine = create_engine(f"sqlite:///{config.DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Scan(Base):
    """One full audit pass (inventory + CUR + rules)."""

    __tablename__ = "scans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="running")  # running|done|failed
    label: Mapped[str] = mapped_column(String(64), default="audit")  # audit|re-audit|baseline
    mode: Mapped[str] = mapped_column(String(32), default="demo")
    account_id: Mapped[str] = mapped_column(String(32), default="")
    region: Mapped[str] = mapped_column(String(32), default="")
    cur_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    inventory_monthly: Mapped[float] = mapped_column(Float, default=0.0)
    reconciliation_pct: Mapped[float] = mapped_column(Float, default=0.0)
    resources_scanned: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    rule_id: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(128))
    service: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(128))
    resource_name: Mapped[str] = mapped_column(String(128), default="")
    evidence: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    monthly_savings: Mapped[float] = mapped_column(Float, default=0.0)
    annual_savings: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(16))  # critical|high|medium|low
    risk_tier: Mapped[int] = mapped_column(Integer, default=0)  # 0..3
    effort: Mapped[str] = mapped_column(String(16), default="low")
    remediation_type: Mapped[str] = mapped_column(String(24))  # delete|modify|configure|stop|plan
    remediation_action: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="identified")  # identified|applied|pending|planned|skipped
    terraform_available: Mapped[bool] = mapped_column(Boolean, default=False)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class RemediationAction(Base):
    __tablename__ = "remediation_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(Integer, index=True)
    scan_id: Mapped[int] = mapped_column(Integer)
    acted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    rule_id: Mapped[str] = mapped_column(String(48))
    resource_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(Text)
    api_calls: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of {api, params, result}
    before_state: Mapped[str] = mapped_column(Text, default="")
    after_state: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")


class PipelineState(Base):
    """Singleton-ish row tracking the demo pipeline for the dashboard."""

    __tablename__ = "pipeline_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    step: Mapped[str] = mapped_column(String(48), default="idle")
    detail: Mapped[str] = mapped_column(Text, default="")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")


def init_db() -> None:
    Base.metadata.create_all(engine)


def reset_db() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def latest_scan(label: str | None = None) -> Scan | None:
    with SessionLocal() as s:
        q = s.query(Scan)
        if label:
            q = q.filter(Scan.label == label)
        return q.order_by(desc(Scan.id)).first()


def scan_findings(scan_id: int) -> list[Finding]:
    with SessionLocal() as s:
        return s.query(Finding).filter(Finding.scan_id == scan_id).all()


def summary_payload(scan: Scan) -> dict:
    return json.loads(scan.summary_json or "{}")

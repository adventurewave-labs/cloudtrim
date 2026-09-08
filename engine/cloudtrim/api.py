"""FastAPI service powering the CloudTrim dashboard.

Endpoints (all read from the SQLite audit store; mutations trigger engine
pipelines in background threads):
  GET  /api/status            engine + pipeline state
  GET  /api/summary           headline numbers, before/after, reconciliation
  GET  /api/findings          findings table (filterable)
  GET  /api/findings/{id}     single finding with evidence + detail
  GET  /api/scans             scan history
  GET  /api/timeline          pipeline step timeline
  GET  /api/pricing           pricing table snapshot
  GET  /api/catalog           rules catalog metadata
  GET  /api/report            download the generated PDF report
  POST /api/audit/run         run audit (background)
  POST /api/remediate/run     run remediation (background)
  POST /api/report/generate   regenerate the PDF report
"""
from __future__ import annotations

import json
import threading
import traceback

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__, config, db, pricing, rules

app = FastAPI(title="CloudTrim Audit Engine", version=__version__)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_bg_lock = threading.Lock()
_bg: dict = {"running": False, "job": None, "error": None, "log": []}


def _bg_run(job: str, fn) -> dict:
    if _bg_lock.locked():
        raise HTTPException(409, "a pipeline job is already running")
    _bg["running"] = True
    _bg["job"] = job
    _bg["error"] = None

    def wrapper():
        try:
            fn()
            _bg["log"].append(f"{job}: done")
        except Exception as exc:
            _bg["error"] = f"{job}: {exc}\n{traceback.format_exc()}"
        finally:
            _bg["running"] = False

    threading.Thread(target=wrapper, daemon=True).start()
    return {"started": job}


@app.get("/api/status")
def status():
    db.init_db()
    with db.SessionLocal() as s:
        state = s.query(db.PipelineState).first()
        scans = s.query(db.Scan).order_by(db.Scan.id.desc()).limit(5).all()
    return {
        "product": __version__,
        "engine": config.mode_label(),
        "background": {"running": _bg["running"], "job": _bg["job"], "error": _bg["error"]},
        "pipeline_step": state.step if state else "idle",
        "pipeline_detail": state.detail if state else "",
        "timeline": json.loads(state.steps_json or "[]") if state else [],
        "scans": [{"id": sc.id, "label": sc.label, "phase": sc.phase} for sc in scans],
        "db_path": str(config.DB_PATH),
        "report_path": str(config.REPORT_PATH),
    }


@app.get("/api/summary")
def summary():
    db.init_db()
    with db.SessionLocal() as s:
        scans = s.query(db.Scan).order_by(db.Scan.id).all()
        if not scans:
            return {"state": "no-scan"}
        audit_scan = next((sc for sc in reversed(scans) if sc.label == "audit"), scans[0])
        re_scan = next((sc for sc in reversed(scans) if sc.label == "re-audit"), None)
        findings = s.query(db.Finding).filter(db.Finding.scan_id == audit_scan.id).all()
        re_findings = s.query(db.Finding).filter(
            db.Finding.scan_id == re_scan.id).all() if re_scan else []
        actions = s.query(db.RemediationAction).filter(
            db.RemediationAction.scan_id == audit_scan.id).all()
    pre = json.loads(audit_scan.summary_json or "{}")
    post = json.loads(re_scan.summary_json or "[]") if re_scan else {}
    applied_now = sum(f.monthly_savings for f in findings if f.status == "applied")
    return {
        "state": "ok",
        "audit_scan_id": audit_scan.id,
        "re_audit_scan_id": re_scan.id if re_scan else None,
        "pre": pre,
        "post": post,
        "reduction_verified_pct": round(
            (pre.get("cur_monthly", 0) - post.get("cur_monthly", 0))
            / pre.get("cur_monthly", 1) * 100, 2) if post else None,
        "findings": {
            "total": len(findings),
            "applied": sum(1 for f in findings if f.status == "applied"),
            "pending": sum(1 for f in findings if f.status == "pending"),
            "planned": sum(1 for f in findings if f.status == "planned"),
        },
        "applied_savings_monthly": round(applied_now, 2),
        "actions": [
            {"rule_id": a.rule_id, "resource": a.resource_id, "action": a.action,
             "verified": a.verified, "before": a.before_state, "after": a.after_state,
             "at": a.acted_at.isoformat()}
            for a in actions
        ],
        "re_scan_findings": len(re_findings),
    }


@app.get("/api/findings")
def findings(scan_id: int | None = None, status: str | None = None,
             service: str | None = None, tier: int | None = None):
    db.init_db()
    with db.SessionLocal() as s:
        q = s.query(db.Finding)
        if scan_id:
            q = q.filter(db.Finding.scan_id == scan_id)
        else:
            latest = s.query(db.Scan).filter(db.Scan.label == "audit").order_by(
                db.Scan.id.desc()).first()
            if not latest:
                latest = s.query(db.Scan).order_by(db.Scan.id.desc()).first()
            if latest:
                q = q.filter(db.Finding.scan_id == latest.id)
        if status:
            q = q.filter(db.Finding.status == status)
        if service:
            q = q.filter(db.Finding.service == service)
        if tier is not None:
            q = q.filter(db.Finding.risk_tier == tier)
        rows = q.order_by(db.Finding.monthly_savings.desc()).all()
    return [_finding_payload(f) for f in rows]


@app.get("/api/findings/{finding_id}")
def finding_detail(finding_id: int):
    db.init_db()
    with db.SessionLocal() as s:
        f = s.get(db.Finding, finding_id)
        if not f:
            raise HTTPException(404, "finding not found")
        actions = s.query(db.RemediationAction).filter(
            db.RemediationAction.finding_id == finding_id).all()
    payload = _finding_payload(f)
    payload["actions"] = [
        {"action": a.action, "api_calls": json.loads(a.api_calls or "[]"),
         "before": a.before_state, "after": a.after_state, "verified": a.verified,
         "at": a.acted_at.isoformat()}
        for a in actions
    ]
    return payload


def _finding_payload(f) -> dict:
    d = json.loads(f.detail_json or "{}")
    d.update({
        "id": f.id,
        "scan_id": f.scan_id,
        "rule_id": f.rule_id,
        "title": f.title,
        "service": f.service,
        "resource_id": f.resource_id,
        "resource_name": f.resource_name,
        "evidence": json.loads(f.evidence or "[]"),
        "monthly_savings": f.monthly_savings,
        "annual_savings": f.annual_savings,
        "severity": f.severity,
        "risk_tier": f.risk_tier,
        "effort": f.effort,
        "remediation_type": f.remediation_type,
        "remediation_action": f.remediation_action,
        "status": f.status,
        "terraform_available": f.terraform_available,
    })
    return d


@app.get("/api/scans")
def scans():
    db.init_db()
    with db.SessionLocal() as s:
        rows = s.query(db.Scan).order_by(db.Scan.id).all()
    return [{
        "id": sc.id, "label": sc.label, "phase": sc.phase,
        "started_at": sc.started_at.isoformat() if sc.started_at else None,
        "finished_at": sc.finished_at.isoformat() if sc.finished_at else None,
        "cur_monthly": sc.cur_monthly,
        "inventory_monthly": sc.inventory_monthly,
        "reconciliation_pct": sc.reconciliation_pct,
        "resources_scanned": sc.resources_scanned,
        "summary": json.loads(sc.summary_json or "{}"),
    } for sc in rows]


@app.get("/api/timeline")
def timeline():
    db.init_db()
    with db.SessionLocal() as s:
        state = s.query(db.PipelineState).first()
    return {"steps": json.loads(state.steps_json or "[]") if state else []}


@app.get("/api/pricing")
def pricing_table():
    book = pricing.PriceBook()
    return {
        "region": book.region,
        "ec2": book.ec2,
        "ebs": book.ebs,
        "snapshot_gb_month": book.snapshot_gb_month,
        "eip_idle_month": book.eip_idle_month,
        "nat_month": book.nat_month,
        "alb_month": book.alb_month,
        "s3": book.s3,
        "hours_per_month": book.hours_per_month,
        "source": book.source,
    }


@app.get("/api/catalog")
def catalog():
    return {"rules": rules.catalog_payload(), "tiers": rules.TIER_LABELS}


@app.get("/api/report")
def get_report():
    if not config.REPORT_PATH.exists():
        raise HTTPException(404, "report not generated yet")
    return FileResponse(config.REPORT_PATH, media_type="application/pdf",
                        filename="cloudtrim-audit-report.pdf")


@app.post("/api/audit/run")
def run_audit_bg():
    from . import audit
    return _bg_run("audit", lambda: audit.run_audit(label="audit"))


@app.post("/api/remediate/run")
def run_remediation_bg():
    from . import remediate

    def job():
        remediate.run_remediation()
    return _bg_run("remediate", job)


@app.post("/api/report/generate")
def generate_report_bg():
    from . import report
    return _bg_run("report", report.generate)

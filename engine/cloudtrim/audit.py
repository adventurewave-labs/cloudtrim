"""Audit pipeline: inventory scan + CUR ingestion + rule evaluation -> findings.

Also runs the reconciliation check that keeps the engine honest: the monthly
run-rate derived from CUR billing data is compared against a bottom-up
cost model built from live API inventory x list pricing. In the demo the
two agree by construction; in live audits a large variance flags CUR lag
(e.g. resources created minutes ago) or pricing-table drift — both worth
surfacing instead of silently trusting one source.
"""
from __future__ import annotations

import datetime as dt
import json

from . import aws, config, cur as curmod, db, pricing, rules
from .scanner import Inventory, scan_inventory


def inventory_monthly_cost(inv: Inventory, cur: curmod.CURSummary) -> float:
    """Bottom-up monthly cost model from live API state + pricing table."""
    total = 0.0
    for inst in inv.instances:
        rate = pricing.EC2_ONDEMAND.get(inst.type, 0.0)
        if inst.state == "running":
            total += rate * pricing.HOURS
    for v in inv.volumes:
        total += pricing.ebs_monthly(v.size_gb, v.type)
    # Snapshot storage is incremental (changed blocks only) — the API can't
    # report stored bytes, so billing data is the reference for this slice,
    # exactly as in a live audit where CUR reports actual GB-months.
    total += cur.by_usage_type.get("USW2-EBS:SnapshotUsage", 0.0)
    for a in inv.addresses:
        if not a.associated:
            total += pricing.eip_idle_monthly()
    for lb in inv.load_balancers:
        lcu = 1.0 if "legacy" in lb.name.lower() else 12.0
        total += pricing.alb_monthly(lcu)
    for ng in inv.nat_gateways:
        if ng.state in ("available", "pending"):
            total += pricing.nat_monthly()
    # S3 / CloudWatch / flat lines: no API-readable size -> take from CUR
    s3_usage = sum(v for k, v in cur.by_usage_type.items()
                   if k.startswith("USW2-TimedStorage") or k.startswith("USW2-GlacierIR"))
    cw_usage = cur.by_usage_type.get("USW2-CloudWatch:IngestedBytes", 0.0) + \
        cur.by_usage_type.get("USW2-CloudWatch:StorageBytes", 0.0)
    dt_out = cur.by_usage_type.get("USW2-DataTransfer-Out-Bytes", 0.0)
    other = cur.by_usage_type.get("USW2-CloudWatch:Metrics", 0.0) + \
        cur.by_usage_type.get("USW2-Route53", 0.0) + cur.by_usage_type.get("USW2-KMS", 0.0)
    total += s3_usage + cw_usage + dt_out + other
    return total


def run_audit(label: str = "audit") -> dict:
    """Execute one full audit pass and persist results."""
    db.init_db()
    account = aws.account_id()

    scan = db.Scan(
        label=label,
        mode="live" if config.is_live_mode() else "demo",
        account_id=account,
        region=config.AWS_REGION,
    )
    with db.SessionLocal() as s:
        s.add(scan)
        s.commit()
        scan_id = scan.id

    inv = scan_inventory()
    cur_summary = curmod.load_summary()
    findings = rules.evaluate_all(inv, cur_summary)

    cur_monthly = cur_summary.monthly_run_rate
    inv_monthly = inventory_monthly_cost(inv, cur_summary)
    recon_pct = 0.0
    if cur_monthly > 0:
        recon_pct = round((inv_monthly / cur_monthly - 1.0) * 100.0, 2)

    applied = [f for f in findings if f.status in ("identified",)]
    planned = [f for f in findings if f.status == "planned"]
    savings_applied = sum(f.monthly_savings for f in findings if f.risk_tier <= 1)
    savings_planned = sum(f.monthly_savings for f in findings if f.risk_tier >= 2)

    summary = {
        "label": label,
        "scanned_at": inv.scanned_at,
        "resource_count": inv.resource_count,
        "cur_monthly": cur_monthly,
        "cur_window_days": cur_summary.window_days,
        "inventory_monthly": round(inv_monthly, 2),
        "reconciliation_pct": recon_pct,
        "findings_total": len(findings),
        "savings_applied_monthly": round(savings_applied, 2),
        "savings_applied_annual": round(savings_applied * 12, 2),
        "savings_planned_monthly": round(savings_planned, 2),
        "savings_identified_monthly": round(savings_applied + savings_planned, 2),
        "reduction_if_applied_pct": round(
            savings_applied / cur_monthly * 100, 2) if cur_monthly else 0.0,
        "reduction_total_potential_pct": round(
            (savings_applied + savings_planned) / cur_monthly * 100, 2) if cur_monthly else 0.0,
        "by_severity": _counts(findings, "severity"),
        "by_service": _counts(findings, "service"),
        "by_risk_tier": _counts(findings, "risk_tier"),
        "cur_by_service": cur_summary.by_service,
    }

    with db.SessionLocal() as s:
        scan = s.get(db.Scan, scan_id)
        scan.finished_at = db.utcnow()
        scan.phase = "done"
        scan.cur_monthly = cur_monthly
        scan.inventory_monthly = inv_monthly
        scan.reconciliation_pct = recon_pct
        scan.resources_scanned = inv.resource_count
        scan.summary_json = json.dumps(summary)
        for f in findings:
            s.add(db.Finding(
                scan_id=scan_id,
                rule_id=f.rule_id, title=f.title, service=f.service,
                resource_id=f.resource_id, resource_name=f.resource_name,
                evidence=json.dumps(f.evidence),
                monthly_savings=f.monthly_savings, annual_savings=f.annual_savings,
                severity=f.severity, risk_tier=f.risk_tier, effort=f.effort,
                remediation_type=f.remediation_type,
                remediation_action=f.remediation_action,
                status=f.status, terraform_available=f.terraform_available,
                detail_json=json.dumps(f.to_dict()),
            ))
        s.commit()

    _append_pipeline_step(f"audit:{label}",
                          f"{len(findings)} findings | CUR ${cur_monthly:.2f}/mo | "
                          f"applied-tier savings ${savings_applied:.2f}/mo "
                          f"({summary['reduction_if_applied_pct']}%)")
    return {"scan_id": scan_id, "findings": [f.to_dict() for f in findings], "summary": summary}


def _counts(findings: list[rules.Finding], attr: str) -> dict:
    out: dict[str, int] = {}
    for f in findings:
        key = str(getattr(f, attr))
        out[key] = out.get(key, 0) + 1
    return out


def _append_pipeline_step(step: str, detail: str) -> None:
    with db.SessionLocal() as s:
        state = s.query(db.PipelineState).first()
        if not state:
            state = db.PipelineState()
            s.add(state)
        state.updated_at = db.utcnow()
        state.step = step
        state.detail = detail
        steps = json.loads(state.steps_json or "[]")
        steps.append({
            "step": step, "detail": detail,
            "at": db.utcnow().isoformat(),
        })
        state.steps_json = json.dumps(steps)
        s.commit()

"""The CloudTrim rules catalog — 22 cost-audit rules across 4 risk tiers.

Risk tiers (the zero-downtime methodology):
  Tier 0 — Pure waste removal. Nothing serves traffic; delete is invisible.
  Tier 1 — Live, non-disruptive changes (in-place API modifications).
  Tier 2 — Scheduled maintenance (minutes, maintenance window).
  Tier 3 — Planned initiatives (days-weeks, client decision).

Tier 0+1 findings are auto-remediable during the audit week with zero
customer-facing downtime. Tier 2+3 are identified, costed, and delivered as
a scheduled roadmap.

Savings math uses the pricing table; evidence comes from live API state
(inventory + CloudWatch) and the CUR billing file. See PRD appendix for
the full catalog specification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import config, pricing
from .cur import CURSummary
from .scanner import Inventory

TIER_LABELS = {
    0: "Tier 0 — pure waste (zero risk)",
    1: "Tier 1 — live change (zero downtime)",
    2: "Tier 2 — scheduled maintenance",
    3: "Tier 3 — planned initiative",
}


@dataclass
class Finding:
    rule_id: str
    title: str
    service: str
    resource_id: str
    resource_name: str
    evidence: list[str] = field(default_factory=list)
    monthly_savings: float = 0.0
    severity: str = "medium"          # critical|high|medium|low
    risk_tier: int = 1
    effort: str = "low"               # low|medium|high
    remediation_type: str = "modify"  # delete|stop|modify|configure|plan
    remediation_action: str = ""
    status: str = "identified"        # identified|applied|pending|planned
    terraform_available: bool = False
    detail: dict = field(default_factory=dict)

    @property
    def annual_savings(self) -> float:
        return round(self.monthly_savings * 12, 2)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "service": self.service,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "evidence": self.evidence,
            "monthly_savings": round(self.monthly_savings, 2),
            "annual_savings": self.annual_savings,
            "severity": self.severity,
            "risk_tier": self.risk_tier,
            "risk_label": TIER_LABELS[self.risk_tier],
            "effort": self.effort,
            "remediation_type": self.remediation_type,
            "remediation_action": self.remediation_action,
            "status": self.status,
            "terraform_available": self.terraform_available,
            "detail": self.detail,
        }


@dataclass
class Rule:
    rule_id: str
    title: str
    service: str
    evaluate: Callable[[Inventory, CURSummary], list[Finding]]
    applies_in_demo: bool = True   # False -> live-AWS-only rule (still evaluated, quiet on emulators)
    description: str = ""


def _cw_cpu(inv: Inventory, instance_id: str) -> tuple[float, int]:
    m = inv.cloudwatch.get(instance_id, {}).get("CPUUtilization")
    return (m[0], m[1]) if m else (100.0, 0)  # no data -> assume busy (safe)


# ---------------------------------------------------------------------------
# EC2
# ---------------------------------------------------------------------------

def rule_idle_ec2(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for inst in inv.running_instances():
        avg, pts = _cw_cpu(inv, inst.id)
        if pts >= 10 and avg < config.IDLE_CPU_PCT:
            monthly = pricing.ec2_monthly(inst.type)
            attached_ebs = sum(
                v.size_gb * pricing.EBS_GB_MONTH.get(v.type, 0.10)
                for v in inv.volumes if v.attached_to == inst.id)
            out.append(Finding(
                rule_id="EC2-IDLE-STOP", title="Idle EC2 instance (CPU < 5%)",
                service="EC2", resource_id=inst.id, resource_name=inst.name,
                evidence=[
                    f"Average CPUUtilization {avg:.1f}% over {config.CW_LOOKBACK_DAYS} days ({pts} datapoints, CloudWatch)",
                    f"Instance type {inst.type}, state running, AZ {inst.az}",
                    f"Compute run-rate ${monthly:.2f}/mo; attached EBS ${attached_ebs:.2f}/mo continues while stopped",
                ],
                monthly_savings=monthly, severity="critical", risk_tier=1,
                effort="low", remediation_type="stop",
                remediation_action=(
                    f"Stop instance {inst.id} (zero customer impact at <5% CPU). "
                    f"Attached EBS (${attached_ebs:.2f}/mo) remains billable while stopped — "
                    f"snapshot-and-detach is scheduled after a 30-day grace window if it stays idle."),
                detail={"avg_cpu": avg, "datapoints": pts, "instance_type": inst.type,
                        "attached_ebs_monthly": round(attached_ebs, 2)},
            ))
    return out


def rule_rightsize_ec2(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for inst in inv.running_instances():
        avg, pts = _cw_cpu(inv, inst.id)
        if pts < 10:
            continue
        # rule precedence: stateless/batch workloads route to the SPOT rule
        workload = (inst.tags.get("workload") or "").lower()
        if workload in ("stateless", "batch", "worker", "ci") or "worker" in inst.name.lower():
            continue
        target = pricing.RIGHTSIZE_LADDER.get(inst.type)
        if not target or not (config.IDLE_CPU_PCT <= avg < config.RIGHTSIZE_CPU_PCT):
            continue
        save = (pricing.EC2_ONDEMAND[inst.type] - pricing.EC2_ONDEMAND[target]) * pricing.HOURS
        if save <= 0:
            continue
        out.append(Finding(
            rule_id="EC2-RIGHTSIZE", title=f"Rightsizing candidate: {inst.type} -> {target}",
            service="EC2", resource_id=inst.id, resource_name=inst.name,
            evidence=[
                f"Average CPUUtilization {avg:.1f}% over {config.CW_LOOKBACK_DAYS} days — headroom on {inst.type}",
                f"One size down in family: {inst.type} -> {target} keeps {pricing.EC2_ONDEMAND[target] * 4:.0f} vCPU-hours of compute margin",
                f"Post-resize projected utilization ~{avg * (pricing.EC2_ONDEMAND[inst.type] / pricing.EC2_ONDEMAND[target]):.0f}%",
            ],
            monthly_savings=save, severity="medium", risk_tier=2,
            effort="medium", remediation_type="plan",
            remediation_action=(
                f"Stop -> modify instance type to {target} -> start (minutes of downtime inside a "
                f"maintenance window, or rolling replace via ASG for zero downtime)."),
            status="planned",
            detail={"avg_cpu": avg, "target_type": target},
        ))
    return out


def rule_graviton(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for inst in inv.running_instances():
        avg, pts = _cw_cpu(inv, inst.id)
        if pts < 10 or avg < 20:
            continue  # steady-state workloads with real telemetry only
        target = pricing.GRAVITON_MAP.get(inst.type)
        if not target:
            continue
        save = (pricing.EC2_ONDEMAND[inst.type] - pricing.EC2_ONDEMAND[target]) * pricing.HOURS
        out.append(Finding(
            rule_id="EC2-GRAVITON", title=f"Graviton (ARM) migration: {inst.type} -> {target}",
            service="EC2", resource_id=inst.id, resource_name=inst.name,
            evidence=[
                f"Steady workload (avg CPU {avg:.1f}%) on x86 {inst.type}",
                f"{target} delivers ~20% better price-performance (us-west-2 on-demand ${pricing.EC2_ONDEMAND[target]}/hr vs ${pricing.EC2_ONDEMAND[inst.type]}/hr)",
                "Requires ARM-compatible build/AMI; rolling deployment recommended",
            ],
            monthly_savings=save, severity="low", risk_tier=3,
            effort="high", remediation_type="plan",
            remediation_action="Rebuild AMI for arm64, rolling replace via ASG (zero downtime).",
            status="planned",
            detail={"target_type": target},
        ))
    return out


def rule_spot(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for inst in inv.running_instances():
        workload = (inst.tags.get("workload") or "").lower()
        if workload not in ("stateless", "batch", "worker", "ci") and "worker" not in inst.name.lower():
            continue
        save = pricing.EC2_ONDEMAND[inst.type] * pricing.HOURS * pricing.SPOT_DISCOUNT
        out.append(Finding(
            rule_id="EC2-SPOT", title="Spot migration candidate (stateless workload)",
            service="EC2", resource_id=inst.id, resource_name=inst.name,
            evidence=[
                f"Workload tagged 'workload={workload}' — interruption-tolerant",
                f"m5 spot typically clears at ~{int((1 - pricing.SPOT_DISCOUNT) * 100)}% of on-demand (us-west-2)",
                "Instance-interruption handling required (checkpointing or ASG rebalance)",
            ],
            monthly_savings=save, severity="low", risk_tier=3,
            effort="medium", remediation_type="plan",
            remediation_action="Move capacity to spot via ASG mixed-instances policy (on-demand base + spot above).",
            status="planned",
        ))
    return out


def rule_savings_plans(inv: Inventory, cur: CURSummary) -> list[Finding]:
    ondemand_compute = sum(
        pricing.ec2_monthly(i.type) for i in inv.running_instances()
        if "cloudtrim:spot" not in (i.tags or {}))
    if ondemand_compute < 300:
        return []
    save = ondemand_compute * pricing.SAVINGS_PLAN_DISCOUNT
    return [Finding(
        rule_id="SAVINGS-PLANS", title="Compute Savings Plans coverage gap",
        service="EC2", resource_id="account", resource_name="AWS account",
        evidence=[
            f"Stable on-demand compute baseline ${ondemand_compute:.2f}/mo across {len(inv.running_instances())} running instances",
            f"1-yr Compute Savings Plan commits the baseline at ~{int(pricing.SAVINGS_PLAN_DISCOUNT * 100)}% discount",
            "Commitment covers the floor only — burst capacity stays flexible on on-demand",
        ],
        monthly_savings=save, severity="medium", risk_tier=3,
        effort="low", remediation_type="plan",
        remediation_action="Purchase 1-yr Compute Savings Plan sized to the 30-day usage floor.",
        status="planned",
        detail={"ondemand_baseline_monthly": round(ondemand_compute, 2)},
    )]


# ---------------------------------------------------------------------------
# EBS / snapshots / AMIs
# ---------------------------------------------------------------------------

def rule_unattached_volumes(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for v in inv.volumes:
        if v.state != "available":
            continue
        monthly = pricing.ebs_monthly(v.size_gb, v.type)
        guard = v.size_gb >= config.VOLUME_SNAPSHOT_GUARD_GB
        out.append(Finding(
            rule_id="EBS-UNATTACHED", title="Unattached EBS volume",
            service="EBS", resource_id=v.id, resource_name=v.name,
            evidence=[
                f"Volume state 'available' — no attachment to any instance",
                f"{v.size_gb} GB {v.type} at ${pricing.EBS_GB_MONTH.get(v.type, 0.10)}/GB-mo = ${monthly:.2f}/mo",
                f"Billed {cur.days_billed(v.id)} of {cur.window_days} days in the CUR window",
            ] + (["Safety policy: snapshot before delete (>= 100 GB volume)"] if guard else []),
            monthly_savings=monthly, severity="high", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action=(
                ("Create safety snapshot, then " if guard else "")
                + f"delete volume {v.id}. Nothing mounts this volume; removal is invisible to workloads."),
            detail={"size_gb": v.size_gb, "volume_type": v.type, "snapshot_guard": guard},
        ))
    return out


def rule_gp2_to_gp3(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for v in inv.volumes:
        if v.type != "gp2":
            continue
        if v.state != "in-use":
            # unattached gp2 volumes are the EBS-UNATTACHED rule's territory:
            # converting a volume that should be deleted is wasted motion
            continue
        if v.size_gb < 20:
            # default 8 GB root volumes: ~$1.60/mo savings — below the
            # engagement's minimum-action threshold; flagged in the catalog
            # but not actionable at fixed-fee economics
            continue
        save = v.size_gb * (pricing.EBS_GB_MONTH["gp2"] - pricing.EBS_GB_MONTH["gp3"])
        out.append(Finding(
            rule_id="EBS-GP2-GP3", title="gp2 -> gp3 volume conversion",
            service="EBS", resource_id=v.id, resource_name=v.name,
            evidence=[
                f"{v.size_gb} GB gp2 volume — gp3 is 20% cheaper per GB-month "
                f"(${pricing.EBS_GB_MONTH['gp2']} -> ${pricing.EBS_GB_MONTH['gp3']})",
                "modify_volume converts live with no detach, no downtime (AWS in-place operation)",
                f"gp3 baseline 3000 IOPS / 125 MBps — at or above gp2 burst performance for most volumes",
            ],
            monthly_savings=save, severity="medium", risk_tier=1,
            effort="low", remediation_type="modify",
            remediation_action=f"modify_volume --volume-type gp3 (live, in-place, zero downtime)",
            terraform_available=True,
            detail={"size_gb": v.size_gb},
        ))
    return out


def rule_oversized_volumes(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for v in inv.volumes:
        if v.state != "in-use":
            continue
        ops = inv.volume_ops.get(v.id)
        if not ops or ops[0] >= 1.0:
            continue
        avg_ops, pts = ops
        target_size = max(50, v.size_gb // 4)
        save = (v.size_gb - target_size) * pricing.EBS_GB_MONTH["gp3"]
        if save <= 0:
            continue
        out.append(Finding(
            rule_id="EBS-OVERSIZED", title="Oversized EBS volume (near-zero I/O)",
            service="EBS", resource_id=v.id, resource_name=v.name,
            evidence=[
                f"Average {avg_ops:.2f} read+write ops/sec over {config.CW_LOOKBACK_DAYS} days ({pts} datapoints)",
                f"{v.size_gb} GB provisioned; steady-state fits ~{target_size} GB",
                "Shrink requires a snapshot->new-volume swap (offline for that volume only)",
            ],
            monthly_savings=save, severity="low", risk_tier=2,
            effort="medium", remediation_type="plan",
            remediation_action="Snapshot -> create smaller gp3 volume -> swap attachment in a maintenance window.",
            status="planned",
            detail={"avg_ops_sec": round(avg_ops, 3), "target_size_gb": target_size},
        ))
    return out


def rule_stale_snapshots(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    ec2 = aws.client("ec2")
    # Snapshots backing AMIs belong to the AMI-STALE rule; skipping them here
    # prevents double-counting savings across the two rules.
    ami_snap_ids: set[str] = set()
    try:
        for page in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
            for ami in page.get("Images", []):
                for bdm in ami.get("BlockDeviceMappings", []):
                    sid = bdm.get("Ebs", {}).get("SnapshotId")
                    if sid:
                        ami_snap_ids.add(sid)
    except Exception:
        pass
    out: list[Finding] = []
    for s in inv.snapshots:
        if s.id in ami_snap_ids:
            continue
        billed = cur.days_billed(s.id)
        if billed < config.SNAPSHOT_STALE_DAYS:
            continue
        monthly = pricing.snapshot_monthly(s.volume_size_gb)
        out.append(Finding(
            rule_id="SNAP-STALE", title="Stale EBS snapshot",
            service="EBS", resource_id=s.id, resource_name=s.description[:60],
            evidence=[
                f"Snapshot billed {billed} of {cur.window_days} days in the CUR window "
                f"(continuously since {cur.first_day_by_resource.get(s.id, '?')})",
                f"{s.volume_size_gb} GB incremental storage at ${pricing.SNAPSHOT_GB_MONTH}/GB-mo = ${monthly:.2f}/mo",
                "No AMI or recent restore references this snapshot",
            ],
            monthly_savings=monthly, severity="medium", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action=f"Delete snapshot {s.id} (pure storage waste; deletion is invisible).",
        ))
    return out


def rule_stale_amis(inv: Inventory, cur: CURSummary) -> list[Finding]:
    ec2 = None
    from . import aws
    ec2 = aws.client("ec2")
    used_images = {i.image_id for i in inv.instances}
    out: list[Finding] = []
    for ami in inv.images:
        if ami.id in used_images or ami.state != "available":
            continue
        snaps = []
        try:
            detail = ec2.describe_images(ImageIds=[ami.id])["Images"][0]
            for bdm in detail.get("BlockDeviceMappings", []):
                if "Ebs" in bdm and bdm["Ebs"].get("SnapshotId"):
                    snaps.append(bdm["Ebs"]["SnapshotId"])
        except Exception:
            continue
        billed = [cur.days_billed(sid) for sid in snaps]
        if not snaps or not any(b >= config.SNAPSHOT_STALE_DAYS for b in billed):
            continue
        snap_sizes = {s.id: s.volume_size_gb for s in inv.snapshots}
        monthly = sum(pricing.snapshot_monthly(snap_sizes.get(sid, 0)) for sid in snaps)
        out.append(Finding(
            rule_id="AMI-STALE", title="Unused AMI + backing snapshots",
            service="EC2", resource_id=ami.id, resource_name=ami.name,
            evidence=[
                f"AMI not referenced by any running instance or launch template",
                f"{len(snaps)} backing snapshots billed continuously "
                f"({', '.join(f'{b}d' for b in billed)})",
                f"Snapshot storage ${monthly:.2f}/mo",
            ],
            monthly_savings=monthly, severity="medium", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action=f"Deregister AMI {ami.id} and delete its {len(snaps)} backing snapshots.",
            detail={"snapshots": snaps},
        ))
    return out


def rule_stopped_terminate(inv: Inventory, cur: CURSummary) -> list[Finding]:
    """Instances stopped by us get a 30-day grace tag before termination."""
    import datetime as dt
    out: list[Finding] = []
    for inst in inv.instances:
        if inst.state != "stopped":
            continue
        stopped_at = inst.tags.get("cloudtrim:stopped-at")
        if stopped_at:
            try:
                grace = (dt.datetime.now(dt.timezone.utc)
                         - dt.datetime.fromisoformat(stopped_at)).days
                if grace < 30:
                    continue  # inside cloudtrim grace window
            except ValueError:
                pass
        if cur.days_billed(inst.id) > 0:  # still burning compute hours -> not truly stopped
            continue
        attached = [v for v in inv.volumes if v.attached_to == inst.id]
        ebs = sum(pricing.ebs_monthly(v.size_gb, v.type) for v in attached)
        if ebs <= 0:
            continue
        out.append(Finding(
            rule_id="EC2-STOPPED-ZOMBIE", title="Long-stopped instance holding EBS ransom",
            service="EC2", resource_id=inst.id, resource_name=inst.name,
            evidence=[
                f"Instance stopped for >= 30 days with zero compute hours in the CUR window",
                f"{len(attached)} attached volumes still billing ${ebs:.2f}/mo of EBS",
            ],
            monthly_savings=ebs, severity="medium", risk_tier=2,
            effort="low", remediation_type="plan",
            remediation_action="Snapshot attached volumes, then terminate instance and delete volumes after sign-off.",
            status="planned",
        ))
    return out


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

def rule_orphan_eips(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for a in inv.addresses:
        if a.associated:
            continue
        out.append(Finding(
            rule_id="EIP-ORPHAN", title="Orphaned Elastic IP",
            service="VPC", resource_id=a.allocation_id, resource_name=a.public_ip,
            evidence=[
                f"EIP {a.public_ip} not associated to any instance/NAT/ALB",
                f"Idle EIPs bill ${pricing.EIP_IDLE_HOUR}/hr = ${pricing.eip_idle_monthly():.2f}/mo",
            ],
            monthly_savings=pricing.eip_idle_monthly(), severity="low", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action=f"Release address {a.public_ip} (reallocate later in seconds if needed).",
        ))
    return out


def rule_idle_elb(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for lb in inv.load_balancers:
        if lb.healthy_targets > 0 or lb.total_targets > 0:
            continue
        usage = cur.by_resource_usage.get(lb.arn, {})
        base = usage.get("USW2-LoadBalancerUsage", 0.0)
        lcu = usage.get("USW2-LCUUsage", 0.0)
        monthly = (base + lcu) if (base or lcu) else pricing.alb_monthly(1.0)
        if monthly <= 0:
            monthly = pricing.alb_monthly(1.0)
        out.append(Finding(
            rule_id="ELB-IDLE", title="Load balancer with no healthy targets",
            service="ELB", resource_id=lb.arn, resource_name=lb.name,
            evidence=[
                f"Target groups contain {lb.total_targets} targets, 0 healthy",
                f"CUR shows ${monthly:.2f}/mo of LB hours + LCU charges with no traffic",
            ],
            monthly_savings=monthly, severity="high", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action=f"Delete load balancer {lb.name} and its empty target groups (receives no traffic).",
            detail={"target_groups": lb.target_groups},
        ))
    return out


def rule_nat_consolidation(inv: Inventory, cur: CURSummary) -> list[Finding]:
    by_vpc: dict[str, list] = {}
    for ng in inv.nat_gateways:
        if ng.state in ("available", "pending"):
            by_vpc.setdefault(ng.vpc_id, []).append(ng)
    out: list[Finding] = []
    for vpc_id, gates in by_vpc.items():
        if len(gates) < 2:
            continue
        save = pricing.nat_monthly() * (len(gates) - 1)
        out.append(Finding(
            rule_id="NAT-CONSOLIDATE", title="Redundant NAT gateways per VPC",
            service="VPC", resource_id=vpc_id, resource_name=f"vpc-{vpc_id[:12]}",
            evidence=[
                f"{len(gates)} NAT gateways active in one VPC "
                f"({', '.join(g.id for g in gates)})",
                f"Each gateway bills ${pricing.nat_monthly():.2f}/mo + data processing",
                "Outbound egress from one gateway covers multi-AZ traffic after route consolidation",
            ],
            monthly_savings=save, severity="medium", risk_tier=2,
            effort="medium", remediation_type="plan",
            remediation_action="Consolidate route tables onto one NAT gateway, delete the rest (minutes of egress blip).",
            status="planned",
            detail={"gateways": [g.id for g in gates]},
        ))
    return out


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def rule_s3_lifecycle(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for b in inv.buckets:
        if b.has_lifecycle:
            continue
        usage = cur.by_resource_usage.get(b.name, {})
        storage_monthly = usage.get("USW2-TimedStorage-ByteHrs", 0.0)
        if storage_monthly < config.S3_LIFECYCLE_MIN_MONTHLY:
            continue
        save = storage_monthly * pricing.S3_LIFECYCLE_SAVINGS_FRACTION
        est_gb = storage_monthly / pricing.S3_GB_MONTH["standard"]
        out.append(Finding(
            rule_id="S3-LIFECYCLE", title="S3 bucket without lifecycle policy",
            service="S3", resource_id=b.name, resource_name=b.name,
            evidence=[
                f"No lifecycle configuration on bucket (get_bucket_lifecycle_configuration -> NoSuchLifecycleConfiguration)",
                f"~{est_gb:,.0f} GB in Standard class at ${pricing.S3_GB_MONTH['standard']}/GB-mo = ${storage_monthly:.2f}/mo",
                f"Transitioning {int(pricing.S3_LIFECYCLE_COLD_FRACTION * 100)}% cold data to Glacier IR "
                f"(${pricing.S3_GB_MONTH['glacier_ir']}/GB-mo) saves ~{int(pricing.S3_LIFECYCLE_SAVINGS_FRACTION * 100)}% blended",
            ],
            monthly_savings=save, severity="medium", risk_tier=1,
            effort="low", remediation_type="configure",
            remediation_action="Apply lifecycle: Intelligent-Tiering + Standard-IA at 30d + Glacier IR at 90d (non-disruptive).",
            terraform_available=True,
            detail={"storage_monthly": round(storage_monthly, 2), "est_gb": round(est_gb, 1)},
        ))
    return out


def rule_s3_mpu_leak(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for b in inv.buckets:
        if not b.incomplete_mpu:
            continue
        usage = cur.by_resource_usage.get(b.name, {})
        mpu_monthly = usage.get("USW2-TimedStorage-GLACIER-ByteHrs", 0.0)
        est_gb = mpu_monthly / pricing.S3_GB_MONTH["standard"]
        out.append(Finding(
            rule_id="S3-MPU-LEAK", title="Incomplete multipart uploads billing as storage",
            service="S3", resource_id=b.name, resource_name=b.name,
            evidence=[
                f"{len(b.incomplete_mpu)} incomplete multipart uploads listed "
                f"(oldest: {b.incomplete_mpu[0]['initiated'][:10] if b.incomplete_mpu else '?'})",
                f"~{est_gb:,.0f} GB of uncommitted parts at ${pricing.S3_GB_MONTH['standard']}/GB-mo = ${mpu_monthly:.2f}/mo",
                "Parts of failed/aborted uploads bill forever until explicitly aborted",
            ],
            monthly_savings=mpu_monthly, severity="high", risk_tier=0,
            effort="low", remediation_type="delete",
            remediation_action="Abort incomplete multipart uploads + add AbortIncompleteMultipartUpload lifecycle rule.",
            detail={"uploads": [u["upload_id"] for u in b.incomplete_mpu][:10]},
        ))
    return out


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------

def rule_logs_retention(inv: Inventory, cur: CURSummary) -> list[Finding]:
    out: list[Finding] = []
    for lg in inv.log_groups:
        if lg.retention_days != 0:
            continue
        usage = cur.by_resource_usage.get(lg.name, {})
        storage_monthly = usage.get("USW2-CloudWatch:StorageBytes", 0.0)
        save = storage_monthly * 0.60
        if save < 2:
            continue
        out.append(Finding(
            rule_id="CW-LOGS-RETENTION", title="Log group never expires",
            service="CloudWatch", resource_id=lg.name, resource_name=lg.name,
            evidence=[
                "retentionInDays = Never Expire (describe_log_groups)",
                f"Stored log bytes accumulate unbounded — ${storage_monthly:.2f}/mo today, grows with ingest",
                f"{config.LOG_RETENTION_DAYS}-day retention bounds storage at ~40% of current footprint",
            ],
            monthly_savings=save, severity="medium", risk_tier=1,
            effort="low", remediation_type="configure",
            remediation_action=f"put_retention_policy({config.LOG_RETENTION_DAYS} days) — applies to new+existing streams.",
            detail={"storage_monthly": round(storage_monthly, 2)},
        ))
    return out


# ---------------------------------------------------------------------------
# Live-mode rules (real AWS; quiet on demo emulators by absence of resources)
# ---------------------------------------------------------------------------

def rule_rds_idle(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    try:
        rds = aws.client("rds")
        dbs = rds.describe_db_instances()["DBInstances"]
    except Exception:
        return []
    out: list[Finding] = []
    cw = aws.client("cloudwatch")
    import datetime as dt
    for db in dbs:
        arn = db["DBInstanceArn"]
        monthly = pricing.EC2_ONDEMAND.get(db["DBInstanceClass"], 0.104) * pricing.HOURS
        monthly += db.get("AllocatedStorage", 20) * pricing.EBS_GB_MONTH["gp2"]
        try:
            stats = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName="CPUUtilization",
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db["DBInstanceIdentifier"]}],
                StartTime=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14),
                EndTime=dt.datetime.now(dt.timezone.utc), Period=86400, Statistics=["Average"])
            pts = stats.get("Datapoints", [])
            avg = sum(p["Average"] for p in pts) / len(pts) if pts else None
        except Exception:
            avg = None
        if avg is not None and avg < 5.0:
            out.append(Finding(
                rule_id="RDS-IDLE", title="Idle RDS instance",
                service="RDS", resource_id=arn, resource_name=db["DBInstanceIdentifier"],
                evidence=[f"Average CPUUtilization {avg:.1f}% over 14 days",
                          f"Instance class {db['DBInstanceClass']}, {db.get('AllocatedStorage', 20)} GB storage"],
                monthly_savings=monthly, severity="high", risk_tier=1,
                effort="low", remediation_type="stop",
                remediation_action="Snapshot, then stop the instance (bills compute only while running).",
            ))
    return out


def rule_rds_gp3(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    try:
        rds = aws.client("rds")
        dbs = rds.describe_db_instances()["DBInstances"]
    except Exception:
        return []
    out: list[Finding] = []
    for db in dbs:
        if db.get("StorageType") != "gp2":
            continue
        gb = db.get("AllocatedStorage", 20)
        save = gb * (pricing.EBS_GB_MONTH["gp2"] - 0.085)  # RDS gp3 storage rate
        out.append(Finding(
            rule_id="RDS-GP3", title="RDS gp2 -> gp3 storage",
            service="RDS", resource_id=db["DBInstanceArn"], resource_name=db["DBInstanceIdentifier"],
            evidence=[f"{gb} GB gp2 storage on {db['DBInstanceIdentifier']}",
                      "RDS gp3 is ~26% cheaper per GB and converts in-place (zero downtime)"],
            monthly_savings=save, severity="low", risk_tier=1,
            effort="low", remediation_type="modify",
            remediation_action="modify_db_instance --storage-type gp3 (live conversion).",
        ))
    return out


def rule_elasticache_idle(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    try:
        elc = aws.client("elasticache")
        clusters = elc.describe_cache_clusters(ShowCacheNodeInfo=True)["CacheClusters"]
    except Exception:
        return []
    out: list[Finding] = []
    for c in clusters:
        node_type = c.get("CacheNodeType", "cache.t3.medium")
        rate = {"cache.t3.micro": 0.016, "cache.t3.small": 0.032,
                "cache.t3.medium": 0.064, "cache.m5.large": 0.13}.get(node_type, 0.064)
        monthly = rate * pricing.HOURS * len(c.get("CacheNodes", [{}]))
        out.append(Finding(
            rule_id="ELASTICACHE-IDLE", title="ElastiCache cluster utilization review",
            service="ElastiCache", resource_id=c["ARN"], resource_name=c["CacheClusterId"],
            evidence=[f"Cluster {c['CacheClusterId']} ({node_type} x {len(c.get('CacheNodes', [{}]))})",
                      "Engine utilization should be verified before downsizing (CPU/evictions metrics)"],
            monthly_savings=monthly * 0.5, severity="low", risk_tier=2,
            effort="medium", remediation_type="plan",
            remediation_action="Rightsize node type or trim replicas based on CloudWatch engine metrics.",
            status="planned",
        ))
    return out


def rule_lambda_prov_concurrency(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    try:
        lam = aws.client("lambda")
        funcs = lam.list_functions()["Functions"]
    except Exception:
        return []
    out: list[Finding] = []
    for f in funcs:
        conc = (f.get("Concurrency") or {}).get("ReservedConcurrentExecutions")
        if not conc:
            continue
        out.append(Finding(
            rule_id="LAMBDA-CONCURRENCY", title="Provisioned/reserved concurrency without traffic check",
            service="Lambda", resource_id=f["FunctionArn"], resource_name=f["FunctionName"],
            evidence=[f"Reserved concurrency {conc} on {f['FunctionName']}",
                      "Reserved concurrency blocks scaling headroom for other functions; verify invocations"],
            monthly_savings=0.0, severity="low", risk_tier=2,
            effort="low", remediation_type="plan",
            remediation_action="Review invocation metrics; drop reservation if utilization is low.",
            status="planned",
        ))
    return out


def rule_classic_elb(inv: Inventory, cur: CURSummary) -> list[Finding]:
    from . import aws
    try:
        elb = aws.client("elb")
        lbs = elb.describe_load_balancers()["LoadBalancerDescriptions"]
    except Exception:
        return []
    out: list[Finding] = []
    for lb in lbs:
        out.append(Finding(
            rule_id="ELB-CLASSIC", title="Classic Load Balancer (deprecated generation)",
            service="ELB", resource_id=lb["LoadBalancerName"], resource_name=lb["LoadBalancerName"],
            evidence=["Classic ELBs are feature-frozen; ALB/NLB are cheaper per LCU and required for modern features"],
            monthly_savings=0.0, severity="low", risk_tier=2,
            effort="medium", remediation_type="plan",
            remediation_action="Migrate to ALB during the next maintenance window (DNS cutover, zero downtime).",
            status="planned",
        ))
    return out


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

RULES: list[Rule] = [
    Rule("EC2-IDLE-STOP", "Idle EC2 instance (CPU < 5%)", "EC2", rule_idle_ec2,
         description="CloudWatch 14-day average CPU below 5% -> stop instance; compute bills $0 while stopped."),
    Rule("EC2-RIGHTSIZE", "Rightsizing candidate", "EC2", rule_rightsize_ec2,
         description="CPU between 5-20% -> one size down in the same family."),
    Rule("EC2-STOPPED-ZOMBIE", "Long-stopped instance holding EBS", "EC2", rule_stopped_terminate,
         description="Stopped >= 30 days -> snapshot and terminate; EBS keeps billing on stopped instances."),
    Rule("EC2-GRAVITON", "Graviton migration candidate", "EC2", rule_graviton,
         description="Steady x86 workload -> arm64 equivalent at ~20% better price-performance."),
    Rule("EC2-SPOT", "Spot migration candidate", "EC2", rule_spot,
         description="Interruption-tolerant workload -> spot capacity at ~62% off."),
    Rule("SAVINGS-PLANS", "Compute Savings Plans coverage gap", "EC2", rule_savings_plans,
         description="Stable on-demand baseline above $300/mo -> 1-yr commitment discount."),
    Rule("EBS-UNATTACHED", "Unattached EBS volume", "EBS", rule_unattached_volumes,
         description="Volume in 'available' state -> snapshot (if >= 100 GB) and delete."),
    Rule("EBS-GP2-GP3", "gp2 -> gp3 conversion", "EBS", rule_gp2_to_gp3,
         description="gp3 is 20% cheaper per GB-month; live in-place conversion via modify_volume."),
    Rule("EBS-OVERSIZED", "Oversized EBS volume", "EBS", rule_oversized_volumes,
         description="Near-zero I/O ops/sec -> shrink via snapshot-swap."),
    Rule("SNAP-STALE", "Stale EBS snapshot", "EBS", rule_stale_snapshots,
         description="Continuously billed >= 45 days with no AMI reference -> delete."),
    Rule("AMI-STALE", "Unused AMI + backing snapshots", "EC2", rule_stale_amis,
         description="AMI not referenced by instances, snapshots billed 45+ days -> deregister + delete."),
    Rule("EIP-ORPHAN", "Orphaned Elastic IP", "VPC", rule_orphan_eips,
         description="Unassociated EIP bills $3.65/mo -> release."),
    Rule("ELB-IDLE", "Load balancer with no healthy targets", "ELB", rule_idle_elb,
         description="0 targets registered -> delete LB and target groups."),
    Rule("NAT-CONSOLIDATE", "Redundant NAT gateways", "VPC", rule_nat_consolidation,
         description="2+ NAT GWs per VPC with low egress -> route consolidation."),
    Rule("S3-LIFECYCLE", "S3 bucket without lifecycle policy", "S3", rule_s3_lifecycle,
         description="No lifecycle config and > $10/mo storage -> transition cold data to cheaper classes."),
    Rule("S3-MPU-LEAK", "Incomplete multipart uploads", "S3", rule_s3_mpu_leak,
         description="Uncommitted upload parts bill as storage forever -> abort + lifecycle guard."),
    Rule("CW-LOGS-RETENTION", "Log group never expires", "CloudWatch", rule_logs_retention,
         description="retentionInDays = Never Expire -> set 90-day retention."),
    Rule("RDS-IDLE", "Idle RDS instance", "RDS", rule_rds_idle,
         description="14-day CPU < 5% -> snapshot and stop. (Live AWS accounts.)"),
    Rule("RDS-GP3", "RDS gp2 -> gp3 storage", "RDS", rule_rds_gp3,
         description="gp2 storage converts to gp3 in-place at ~26% savings. (Live AWS accounts.)"),
    Rule("ELASTICACHE-IDLE", "ElastiCache utilization review", "ElastiCache", rule_elasticache_idle,
         description="Node rightsize based on engine metrics. (Live AWS accounts.)"),
    Rule("LAMBDA-CONCURRENCY", "Reserved concurrency review", "Lambda", rule_lambda_prov_concurrency,
         description="Unneeded concurrency reservation. (Live AWS accounts.)"),
    Rule("ELB-CLASSIC", "Classic Load Balancer migration", "ELB", rule_classic_elb,
         description="Deprecated ELB generation -> ALB migration. (Live AWS accounts.)"),
]


def evaluate_all(inv: Inventory, cur: CURSummary) -> list[Finding]:
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule.evaluate(inv, cur))
        except Exception as exc:  # one broken rule never kills the audit
            findings.append(Finding(
                rule_id=rule.rule_id, title=f"{rule.title} — evaluation error",
                service=rule.service, resource_id="engine", resource_name="rule engine",
                evidence=[f"rule raised {type(exc).__name__}: {exc}"],
                monthly_savings=0.0, severity="low", risk_tier=3,
                remediation_type="plan", remediation_action="Investigate rule failure.",
                status="skipped",
            ))
    findings.sort(key=lambda f: (-f.monthly_savings, f.rule_id))
    return findings


def catalog_payload() -> list[dict]:
    return [
        {
            "rule_id": r.rule_id,
            "title": r.title,
            "service": r.service,
            "description": r.description,
            "demo_visible": r.applies_in_demo,
        }
        for r in RULES
    ]

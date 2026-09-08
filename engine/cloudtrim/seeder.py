"""Demo account seeder.

Two stages, both real:

  1. TERRAFORM — `terraform apply` of engine/terraform/seed/main.tf against
     the configured AWS endpoint (moto in compose/CI, LocalStack Pro, or a
     real AWS scratch account). 25 instances, 30 volumes, 3 orphaned EIPs,
     2 ALBs, 4 S3 buckets, 3 log groups, VPC networking.

  2. POST-SEED (boto3) — the things emulators/IaC can't express:
     CloudWatch CPU + volume-op telemetry (14 days), 22 EBS snapshots with
     billing history, 2 unused AMIs, 2 NAT gateways, 3 incomplete S3
     multipart uploads, and the CUR billing export derived from live API
     state (see cloudtrim.cur).

Everything is recorded in data/seed-spec.json so the CUR generator can
produce billing data consistent with the inventory (S3 bytes, log bytes,
snapshot billing history, flat monthly lines).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from . import aws, config, cur as curmod, db

TERRAFORM_BIN = os.environ.get("TERRAFORM_BIN", "terraform")
SEED_DIR = config.TF_DIR / "seed"

CPU_PROFILE = {
    "prod-api": (35.0, 3.0),      # (mean, jitter)
    "prod-worker": (15.0, 2.0),
    "staging-api": (3.1, 0.4),
    "sandbox": (2.1, 0.3),
    "dev-box": (9.5, 1.2),
}

# daily read+write ops for attached data volumes by prefix
VOL_OPS_PROFILE = {
    "prod-api": 2_000_000,     # ~23 ops/s
    "prod-worker": 50_000,     # ~0.6 ops/s  -> oversized candidates
    "staging-api": 60_000,     # ~0.7 ops/s
    "sandbox": 60_000,
    "dev-box": 60_000,
}

SPEC_FLAT_MONTHLY = [
    {"product": "Amazon EC2", "product_name": "Amazon Elastic Compute Cloud",
     "usage_type": "USW2-DataTransfer-Out-Bytes", "monthly_cost": 180.0},
    {"product": "Amazon CloudWatch", "product_name": "Amazon CloudWatch",
     "usage_type": "USW2-CloudWatch:Metrics", "monthly_cost": 5.0},
    {"product": "Amazon Route53", "product_name": "Amazon Route 53",
     "usage_type": "USW2-Route53", "monthly_cost": 25.0},
    {"product": "AWS KMS", "product_name": "AWS Key Management Service",
     "usage_type": "USW2-KMS", "monthly_cost": 20.0},
    {"product": "AWS CloudTrail", "product_name": "AWS CloudTrail",
     "usage_type": "USW2-CloudTrail", "monthly_cost": 15.0},
]


def _tf(env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TF_IN_AUTOMATION": "1", **env_extra}
    return subprocess.run(
        [TERRAFORM_BIN, *args], cwd=SEED_DIR, env=env,
        capture_output=True, text=True, timeout=900)


def run_terraform_apply(endpoint: str | None) -> str:
    """Apply the seed module. Returns terraform output (for logging)."""
    endpoint = endpoint if endpoint is not None else config.AWS_ENDPOINT_URL
    tf_vars = {"TF_VAR_aws_endpoint": endpoint or ""}
    init = _tf(tf_vars, "init", "-no-color")
    if init.returncode != 0:
        raise RuntimeError(f"terraform init failed:\n{init.stdout}\n{init.stderr}")
    # re-seed cleanly if state exists
    if (SEED_DIR / "terraform.tfstate").exists():
        _tf(tf_vars, "destroy", "-auto-approve", "-no-color")
    apply = _tf(tf_vars, "apply", "-auto-approve", "-no-color")
    if apply.returncode != 0:
        raise RuntimeError(f"terraform apply failed:\n{apply.stdout}\n{apply.stderr}")
    return apply.stdout


def _metrics_for_instances() -> None:
    """14 days of daily CPUUtilization per instance, profiled by Name tag."""
    cw = aws.client("cloudwatch")
    ec2 = aws.client("ec2")
    instances = []
    for page in ec2.get_paginator("describe_instances").paginate():
        for res in page["Reservations"]:
            instances.extend(res["Instances"])
    rnd = random.Random(42)
    base = dt.date.today() - dt.timedelta(days=config.CW_LOOKBACK_DAYS)
    for offset in range(config.CW_LOOKBACK_DAYS):
        day = base + dt.timedelta(days=offset)
        ts = dt.datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=dt.timezone.utc)
        batch: list[dict] = []
        for inst in instances:
            name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
            profile_key = next((k for k in CPU_PROFILE if name.startswith(k)), "prod-api")
            mean, jitter = CPU_PROFILE[profile_key]
            cpu = max(0.1, rnd.gauss(mean, jitter))
            batch.append({
                "MetricName": "CPUUtilization",
                "Dimensions": [{"Name": "InstanceId", "Value": inst["InstanceId"]}],
                "Timestamp": ts, "Value": round(cpu, 3), "Unit": "Percent",
            })
        for i in range(0, len(batch), 20):
            cw.put_metric_data(Namespace="AWS/EC2", MetricData=batch[i:i + 20])
    # volume ops (attached data volumes only; roots have no telemetry -> rule skips)
    volumes = [v for v in ec2.describe_volumes()["Volumes"] if v["Attachments"]]
    for offset in range(config.CW_LOOKBACK_DAYS):
        day = base + dt.timedelta(days=offset)
        ts = dt.datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=dt.timezone.utc)
        batch: list[dict] = []
        for v in volumes:
            vol_name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), "")
            daily_ops = next(
                (ops for prefix, ops in VOL_OPS_PROFILE.items() if vol_name.startswith(prefix)),
                2_000_000)
            read = int(daily_ops * 0.3)
            write = int(daily_ops * 0.7)
            for metric, value in (("VolumeReadOps", read), ("VolumeWriteOps", write)):
                batch.append({
                    "MetricName": metric,
                    "Dimensions": [{"Name": "VolumeId", "Value": v["VolumeId"]}],
                    "Timestamp": ts, "Value": value, "Unit": "Count",
                })
        for i in range(0, len(batch), 20):
            cw.put_metric_data(Namespace="AWS/EBS", MetricData=batch[i:i + 20])


def _seed_snapshots_and_amis() -> dict:
    """22 snapshots (14 stale, 6 AMI-backed, 2 recent) + 2 unused AMIs."""
    ec2 = aws.client("ec2")
    az = f"{config.AWS_REGION}a"
    scratch = ec2.create_volume(AvailabilityZone=az, Size=100, VolumeType="gp3",
                                TagSpecifications=[{"ResourceType": "volume", "Tags": [
                                    {"Key": "Name", "Value": "snapshot-scratch"}]}])["VolumeId"]
    snap_spec: dict = {}

    def snap(description: str, billed_days: int) -> str:
        sid = ec2.create_snapshot(VolumeId=scratch, Description=description)["SnapshotId"]
        snap_spec[sid] = {"billed_days": billed_days, "data_gb": 100}
        return sid

    for i in range(14):
        snap(f"old-backup-{i:02d}", billed_days=60)
    ami_snaps = [snap(f"ami-root-{i}", billed_days=60) for _ in range(2) for i in range(3)]
    for i in range(2):
        snap(f"recent-backup-{i}", billed_days=7)

    for ami_idx in range(2):
        ec2.register_image(
            Name=f"demo-app-v{ami_idx + 1}",
            Description="Superseded application image",
            Architecture="x86_64",
            RootDeviceName="/dev/xvda",
            VirtualizationType="hvm",
            BlockDeviceMappings=[
                {"DeviceName": f"/dev/xvda{'' if i == 0 else chr(98 + i)}",
                 "Ebs": {"SnapshotId": ami_snaps[ami_idx * 3 + i],
                         "VolumeSize": 100, "DeleteOnTermination": False}}
                for i in range(3)
            ],
        )
    ec2.delete_volume(VolumeId=scratch)
    return snap_spec


def _seed_mpu_leaks() -> None:
    s3 = aws.client("s3")
    for i in range(3):
        s3.create_multipart_upload(
            Bucket="cloudtrim-demo-ml-data",
            Key=f"datasets/train-shard-{i:03d}.parquet")


def _seed_nat_gateways() -> list[str]:
    """Two NAT gateways (redundant pair). Skipped gracefully where the
    emulator does not implement NAT gateway creation."""
    ec2 = aws.client("ec2")
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": ["cloudtrim-demo-vpc"]}])["Vpcs"]
    if not vpcs:
        return []
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]}])["Subnets"]]
    created: list[str] = []
    allocations: list[str] = []
    try:
        for subnet in subnets[:2]:
            alloc = ec2.allocate_address(Domain="vpc")["AllocationId"]
            allocations.append(alloc)
            ngw = ec2.create_nat_gateway(SubnetId=subnet, AllocationId=alloc)["NatGateway"]
            created.append(ngw["NatGatewayId"])
    except Exception:
        for alloc in allocations:
            try:
                ec2.release_address(AllocationId=alloc)
            except Exception:
                pass
    return created


def build_spec(snap_spec: dict) -> dict:
    return {
        "buckets": {
            "cloudtrim-demo-assets": {"gb": 3000},
            "cloudtrim-demo-backups": {"gb": 5000},
            "cloudtrim-demo-ml-data": {"gb": 200, "incomplete_mpu_gb": [200.0, 200.0, 200.0]},
            "cloudtrim-demo-logs-archive": {"gb": 50},
        },
        "log_groups": {
            "/ecs/prod-api": {"ingest_gb_month": 30.0, "stored_gb": 950.0},
            "/ecs/staging": {"ingest_gb_month": 4.0, "stored_gb": 80.0},
            "/aws/lambda/demo-api": {"ingest_gb_month": 6.0, "stored_gb": 18.0},
        },
        "snapshots": snap_spec,
        "flat_monthly": SPEC_FLAT_MONTHLY,
    }


def seed(force: bool = False) -> dict:
    """Full demo seed: terraform apply + telemetry + billing + fresh DB."""
    if (config.DATA_DIR / "seed-spec.json").exists() and not force:
        print("seed-spec.json exists; re-run with force=True to re-seed")
    print(f"[seed] target: {config.mode_label()}")

    print("[seed] stage 1/4: terraform apply (wasteful startup account)")
    tf_out = run_terraform_apply(config.AWS_ENDPOINT_URL)
    applied = tf_out.count("Creation complete")
    print(f"[seed] terraform created {applied} resources")

    print("[seed] stage 2/4: telemetry, snapshots, AMIs, MPU leaks, NAT gateways")
    _metrics_for_instances()
    snap_spec = _seed_snapshots_and_amis()
    _seed_mpu_leaks()
    nat_ids = _seed_nat_gateways()
    print(f"[seed] cloudwatch metrics for 14 days; {len(snap_spec)} snapshots; "
          f"2 AMIs; 3 MPU leaks; {len(nat_ids)} NAT gateways")

    print("[seed] stage 3/4: CUR billing export (derived from live API state)")
    spec = build_spec(snap_spec)
    (config.DATA_DIR / "seed-spec.json").write_text(json.dumps(spec, indent=2))
    cur_path = curmod.generate_demo_cur()
    summary = curmod.parse_cur(cur_path)
    print(f"[seed] CUR: {summary.window_days}-day window, "
          f"monthly run-rate ${summary.monthly_run_rate:,.2f}")

    print("[seed] stage 4/4: reset audit database")
    db.reset_db()
    with db.SessionLocal() as s:
        s.add(db.PipelineState(step="seeded",
                               detail=f"terraform:{applied} resources, CUR ${summary.monthly_run_rate:,.2f}/mo"))
        s.commit()
    return {"terraform_resources": applied, "cur_monthly": summary.monthly_run_rate,
            "snapshots": len(snap_spec), "nat_gateways": len(nat_ids)}

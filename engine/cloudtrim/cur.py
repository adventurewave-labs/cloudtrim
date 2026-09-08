"""AWS Cost & Usage Report (CUR) ingestion.

The CUR is the audit's source of truth for money. This module:

  1. PARSES real CUR 2.0 CSV exports (resource-daily or resource-hourly
     aggregation) exactly as AWS delivers them from a CUR-enabled S3 bucket.
  2. GENERATES a CUR for the demo account in the identical schema, derived
     from live API state (describe_* calls) so billing always reconciles
     with the actual inventory — before AND after remediation.

In live mode, ``load_summary`` reads the CUR csv dropped/updated from the
client's S3 CUR bucket (s3://<cur-bucket>/<prefix>/...) via the same parser.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import config, pricing

# CUR 2.0 column subset used by the engine (identity/line_item/product/...)
CUR_COLUMNS = [
    "identity/LineItemId",
    "identity/TimeInterval",
    "line_item/UsageStartDate",
    "line_item/UsageEndDate",
    "bill/BillingEntity",
    "bill/PayerAccountId",
    "line_item/ProductCode",
    "line_item/UsageType",
    "line_item/Operation",
    "line_item/ResourceId",
    "line_item/LineItemType",
    "line_item/UnblendedCost",
    "line_item/BlendedCost",
    "product/ProductName",
    "product/instance_type",
    "product/product_family",
    "resourceTags/user:Name",
    "resourceTags/user:env",
    "resourceTags/user:workload",
]

SERVICE_OF_PRODUCT = {
    "Amazon Elastic Compute Cloud": "EC2",
    "Amazon Elastic Block Store": "EC2 (EBS)",
    "Amazon Simple Storage Service": "S3",
    "Amazon Elastic Load Balancing": "ELB",
    "Amazon CloudWatch": "CloudWatch",
    "Amazon VPC": "VPC",
    "Amazon Route 53": "Route53",
    "AWS Key Management Service": "KMS",
}


@dataclass
class CostLine:
    usage_type: str
    resource_id: str
    service: str
    operation: str = ""
    instance_type: str = ""
    product_name: str = ""
    resource_name: str = ""
    unblended_cost: float = 0.0
    usage_start: str = ""


@dataclass
class CURSummary:
    """Aggregated view of a CUR window."""

    window_days: int = 0
    total_cost: float = 0.0
    monthly_run_rate: float = 0.0
    by_service: dict[str, float] = field(default_factory=dict)
    by_resource: dict[str, float] = field(default_factory=dict)
    by_usage_type: dict[str, float] = field(default_factory=dict)
    by_resource_usage: dict[str, dict[str, float]] = field(default_factory=dict)
    first_day_by_resource: dict[str, str] = field(default_factory=dict)
    billed_days_by_resource: dict[str, int] = field(default_factory=dict)
    lines: list[CostLine] = field(default_factory=list)
    source_path: str = ""

    def days_billed(self, resource_id: str) -> int:
        return self.billed_days_by_resource.get(resource_id, 0)

    def resource_monthly(self, resource_id: str) -> float:
        """Monthly run-rate attributable to one resource."""
        return self.by_resource.get(resource_id, 0.0)


def _parse_float(v: str) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


def parse_cur(path: Path) -> CURSummary:
    """Parse a CUR CSV (resource-daily aggregation) into a CURSummary."""
    lines: list[CostLine] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("line_item/LineItemType", "Usage") not in ("Usage", "RIFee", "Credit", "DiscountedUsage"):
                continue
            lines.append(
                CostLine(
                    usage_type=row.get("line_item/UsageType", ""),
                    resource_id=row.get("line_item/ResourceId", "") or "",
                    service=SERVICE_OF_PRODUCT.get(
                        row.get("product/ProductName", ""),
                        row.get("line_item/ProductCode", "Other"),
                    ),
                    operation=row.get("line_item/Operation", ""),
                    instance_type=row.get("product/instance_type", ""),
                    product_name=row.get("product/ProductName", ""),
                    resource_name=row.get("resourceTags/user:Name", ""),
                    unblended_cost=_parse_float(row.get("line_item/UnblendedCost", "0")),
                    usage_start=(row.get("line_item/UsageStartDate", "") or "")[:10],
                )
            )
    return _aggregate(lines, path)


def _aggregate(lines: list[CostLine], path: Path | None = None) -> CURSummary:
    by_service: dict[str, float] = {}
    by_resource: dict[str, float] = {}
    by_usage_type: dict[str, float] = {}
    by_resource_usage: dict[str, dict[str, float]] = {}
    days_by_resource: dict[str, set[str]] = {}
    dates: set[str] = set()
    for ln in lines:
        by_service[ln.service] = by_service.get(ln.service, 0.0) + ln.unblended_cost
        by_usage_type[ln.usage_type] = by_usage_type.get(ln.usage_type, 0.0) + ln.unblended_cost
        if ln.resource_id:
            by_resource[ln.resource_id] = by_resource.get(ln.resource_id, 0.0) + ln.unblended_cost
            ru = by_resource_usage.setdefault(ln.resource_id, {})
            ru[ln.usage_type] = ru.get(ln.usage_type, 0.0) + ln.unblended_cost
            bucket = days_by_resource.setdefault(ln.resource_id, set())
            if ln.usage_start:
                bucket.add(ln.usage_start)
                dates.add(ln.usage_start)
    window_days = len(dates) or 1
    total = sum(ln.unblended_cost for ln in lines)
    scale = pricing.DAYS_PER_MONTH / window_days
    return CURSummary(
        window_days=window_days,
        total_cost=round(total, 2),
        monthly_run_rate=round(total * scale, 2),
        by_service={k: round(v * scale, 2) for k, v in by_service.items()},
        by_resource={k: round(v * scale, 2) for k, v in by_resource.items()},
        by_usage_type={k: round(v * scale, 2) for k, v in by_usage_type.items()},
        by_resource_usage={
            rid: {ut: round(c * scale, 2) for ut, c in usage.items()}
            for rid, usage in by_resource_usage.items()
        },
        first_day_by_resource={k: min(v) for k, v in days_by_resource.items()},
        billed_days_by_resource={k: len(v) for k, v in days_by_resource.items()},
        lines=lines,
        source_path=str(path) if path else "",
    )


def load_summary() -> CURSummary:
    """Load the most recent CUR csv for the account (demo file or live export)."""
    files = sorted(config.CUR_DIR.glob("*.csv"))
    if not files:
        return CURSummary()
    return parse_cur(files[-1])


# ---------------------------------------------------------------------------
# CUR GENERATION (demo account) — derived from live API state
# ---------------------------------------------------------------------------


def _day_range(days: int) -> list[dt.date]:
    today = dt.date.today()
    return [today - dt.timedelta(days=i) for i in range(days, 0, -1)]


def _name_tag(tags: list[dict] | None) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


def generate_demo_cur(days: int = config.CUR_WINDOW_DAYS, account: str = "123456789012") -> Path:
    """Write a CUR csv for the CURRENT state of the demo account.

    Costs are derived from real describe_* API state (instances, volumes,
    snapshots, EIPs, load balancers, NAT gateways) so the billing file
    always reconciles with inventory. Non-API-measurable lines (S3 stored
    bytes, CloudWatch logs bytes, data transfer) come from the seed spec
    state file written by the seeder.
    """
    from . import aws  # local import to avoid cycles at module load

    ec2 = aws.client("ec2")
    elbv2 = aws.client("elbv2")
    s3 = aws.client("s3")
    logs = aws.client("logs")

    rows: list[dict] = []
    line_no = 0

    def add(day: dt.date, product: str, usage_type: str, operation: str,
            resource_id: str, cost: float, product_name: str = "",
            instance_type: str = "", family: str = "", name: str = "",
            env: str = "", workload: str = "") -> None:
        nonlocal line_no
        line_no += 1
        start = f"{day.isoformat()}T00:00:00Z"
        end = f"{day.isoformat()}T23:59:59Z"
        rows.append({
            "identity/LineItemId": f"{line_no:012d}",
            "identity/TimeInterval": f"{start}/{end}",
            "line_item/UsageStartDate": start,
            "line_item/UsageEndDate": end,
            "bill/BillingEntity": "AWS",
            "bill/PayerAccountId": account,
            "line_item/ProductCode": product,
            "line_item/UsageType": usage_type,
            "line_item/Operation": operation,
            "line_item/ResourceId": resource_id,
            "line_item/LineItemType": "Usage",
            "line_item/UnblendedCost": f"{cost:.6f}",
            "line_item/BlendedCost": f"{cost:.6f}",
            "product/ProductName": product_name,
            "product/instance_type": instance_type,
            "product/product_family": family,
            "resourceTags/user:Name": name,
            "resourceTags/user:env": env,
            "resourceTags/user:workload": workload,
        })

    daily = 1.0 / pricing.DAYS_PER_MONTH  # GB-month / instance-hour proration

    # --- EC2 instances: compute billed only while running -------------------
    instances = []
    for page in ec2.get_paginator("describe_instances").paginate():
        for res in page["Reservations"]:
            instances.extend(res["Instances"])
    for inst in instances:
        itype = inst["InstanceType"]
        rate = pricing.EC2_ONDEMAND.get(itype, 0.0)
        name = _name_tag(inst.get("Tags"))
        running = inst["State"]["Name"] == "running"
        for day in _day_range(days):
            if running:
                add(day, "Amazon Elastic Compute Cloud",
                    f"USW2-BoxUsage:{itype}", "RunInstances:0002",
                    inst["InstanceId"], rate * 24, product_name="Amazon Elastic Compute Cloud",
                    instance_type=itype, family="Compute Instance", name=name)

    # --- EBS volumes ---------------------------------------------------------
    vols = ec2.describe_volumes()["Volumes"]
    for v in vols:
        vtype = v["VolumeType"]
        rate = pricing.EBS_GB_MONTH.get(vtype, 0.10)
        for day in _day_range(days):
            add(day, "Amazon Elastic Compute Cloud",
                f"USW2-EBS:VolumeUsage.{vtype}", "CreateVolume",
                v["VolumeId"], v["Size"] * rate * daily,
                product_name="Amazon Elastic Block Store", family="Storage",
                name=_name_tag(v.get("Tags")))

    # --- EBS snapshots: billed-days + incremental data size from billing state --
    # (emulators report creation-time "now"; billing history lives in the seed
    #  spec / real CUR exports. data_gb = actual stored blocks, not volume size)
    snaps = ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
    spec_snaps = _load_seed_spec().get("snapshots", {})
    for s in snaps:
        entry = spec_snaps.get(s["SnapshotId"], {})
        data_gb = entry.get("data_gb", 0)
        billed_days = min(int(entry.get("billed_days", 1)), days)
        for day in _day_range(billed_days):
            add(day, "Amazon Elastic Compute Cloud",
                "USW2-EBS:SnapshotUsage", "CreateSnapshot",
                s["SnapshotId"], data_gb * pricing.SNAPSHOT_GB_MONTH * daily,
                product_name="Amazon Elastic Block Store", family="Storage")

    # --- Elastic IPs (idle only; attached EIPs are free) --------------------
    addrs = ec2.describe_addresses()["Addresses"]
    for a in addrs:
        if not a.get("AssociationId"):
            for day in _day_range(days):
                add(day, "Amazon VPC", "USW2-IdleAddress", "",
                    a["AllocationId"], pricing.EIP_IDLE_HOUR * 24,
                    product_name="Amazon VPC", family="IP Address")

    # --- Load balancers ------------------------------------------------------
    try:
        lbs = elbv2.describe_load_balancers()["LoadBalancers"]
    except Exception:
        lbs = []
    for lb in lbs:
        lcu = 1.0 if "legacy" in lb["LoadBalancerName"] else 12.0
        for day in _day_range(days):
            add(day, "Amazon Elastic Load Balancing",
                "USW2-LoadBalancerUsage", "CreateLoadBalancer",
                lb["LoadBalancerArn"], pricing.ALB_HOUR * 24,
                product_name="Amazon Elastic Load Balancing", family="Load Balancer")
            add(day, "Amazon Elastic Load Balancing",
                "USW2-LCUUsage", "CreateLoadBalancer",
                lb["LoadBalancerArn"], pricing.ALB_LCU_HOUR * 24 * lcu,
                product_name="Amazon Elastic Load Balancing", family="Load Balancer")

    # --- NAT gateways ---------------------------------------------------------
    try:
        nats = ec2.describe_nat_gateways()["NatGateways"]
    except Exception:
        nats = []
    for ng in nats:
        if ng.get("State") != "deleted":
            for day in _day_range(days):
                add(day, "Amazon VPC", "USW2-NatGateway-Hours", "",
                    ng["NatGatewayId"], pricing.NAT_GATEWAY_HOUR * 24,
                    product_name="Amazon VPC", family="NAT Gateway")

    # --- S3 buckets: storage from seed-spec state (S3 has no size API) ------
    spec = _load_seed_spec()
    for bucket_name, b in spec.get("buckets", {}).items():
        try:
            s3.head_bucket(Bucket=bucket_name)
        except Exception:
            continue
        has_lifecycle = _bucket_has_lifecycle(s3, bucket_name)
        cold_gb = b["gb"] * pricing.S3_LIFECYCLE_COLD_FRACTION if has_lifecycle else 0.0
        warm_gb = b["gb"] - cold_gb
        for day in _day_range(days):
            if warm_gb:
                add(day, "Amazon Simple Storage Service",
                    "USW2-TimedStorage-ByteHrs", "StandardStorage",
                    bucket_name, warm_gb * pricing.S3_GB_MONTH["standard"] * daily,
                    product_name="Amazon Simple Storage Service", family="Storage")
            if cold_gb:
                add(day, "Amazon Simple Storage Service",
                    "USW2-GlacierIR-TimedStorage-ByteHrs", "StandardStorage",
                    bucket_name, cold_gb * pricing.S3_GB_MONTH["glacier_ir"] * daily,
                    product_name="Amazon Simple Storage Service", family="Storage")
        # incomplete multipart uploads (leak) — real line items in the spec
        for mpu in b.get("incomplete_mpu_gb", []):
            for day in _day_range(days):
                add(day, "Amazon Simple Storage Service",
                    "USW2-TimedStorage-GLACIER-ByteHrs", "StandardStorage",
                    bucket_name, mpu * pricing.S3_GB_MONTH["standard"] * daily,
                    product_name="Amazon Simple Storage Service", family="Storage")

    # --- CloudWatch logs ------------------------------------------------------
    for lg_name, lg in spec.get("log_groups", {}).items():
        try:
            resp = logs.describe_log_groups(logGroupNamePrefix=lg_name)["logGroups"]
            if not resp:
                continue
            retention = resp[0].get("retentionInDays")
        except Exception:
            retention = None
        if retention:
            # bounded storage: steady state at ~retention days of ingest
            stored_gb = lg["ingest_gb_month"] / pricing.DAYS_PER_MONTH * min(retention, 90)
        else:
            stored_gb = lg["stored_gb"]
        for day in _day_range(days):
            add(day, "CloudWatch", "USW2-CloudWatch:IngestedBytes", "",
                lg_name, lg["ingest_gb_month"] / pricing.DAYS_PER_MONTH * pricing.CW_LOGS_INGESTION_GB,
                product_name="Amazon CloudWatch", family="Log Stream")
            add(day, "CloudWatch", "USW2-CloudWatch:StorageBytes", "",
                lg_name, stored_gb * pricing.CW_LOGS_STORAGE_GB_MONTH * daily,
                product_name="Amazon CloudWatch", family="Log Stream")

    # --- flat monthly lines (spec): data transfer, metrics, other -----------
    for flat in spec.get("flat_monthly", []):
        per_day = flat["monthly_cost"] / pricing.DAYS_PER_MONTH
        for day in _day_range(days):
            add(day, flat.get("product", "Other"), flat["usage_type"], "",
                flat.get("resource_id", ""), per_day,
                product_name=flat.get("product_name", ""), family="")

    out = config.CUR_DIR / f"{config.DEMO_CUR_PREFIX}-{dt.date.today().isoformat()}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CUR_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    # keep only the newest two CUR files (before/after remediation)
    files = sorted(config.CUR_DIR.glob(f"{config.DEMO_CUR_PREFIX}*.csv"))
    for old in files[:-2]:
        old.unlink()
    return out


def _load_seed_spec() -> dict:
    import json

    spec_path = config.DATA_DIR / "seed-spec.json"
    if spec_path.exists():
        return json.loads(spec_path.read_text())
    return {}


def _bucket_has_lifecycle(s3, bucket: str) -> bool:
    try:
        s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        return True
    except Exception:
        return False


def cur_files() -> list[Path]:
    return sorted(config.CUR_DIR.glob("*.csv"))

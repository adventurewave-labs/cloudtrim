"""AWS list pricing for the audit cost model.

Public on-demand list prices, us-west-2, Linux (unless noted), captured
2024-12. In production deployments this table is refreshed from the AWS
Price List Bulk API / pricing client (see refresh hooks); for the audit
demo it is the static source of truth so CUR generation and savings math
reconcile exactly.

All EC2 prices are USD/hour. EBS/S3 prices are USD per GB-month unless
noted. HOURS_PER_MONTH = 730 is the standard AWS billing convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config

HOURS = config.HOURS_PER_MONTH
DAYS_PER_MONTH = config.DAYS_PER_MONTH

# --- EC2 on-demand (us-west-2, Linux) --------------------------------------
EC2_ONDEMAND = {
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    # Graviton equivalents (in-flight / roadmap savings math)
    "m7g.large": 0.0816,
    "m7g.xlarge": 0.1632,
    "m7g.2xlarge": 0.3264,
    "c7g.xlarge": 0.1384,
    # RDS (live-mode rules only; single-AZ, MySQL, us-west-2)
    "db.t3.micro": 0.026,
    "db.t3.medium": 0.104,
    "db.m5.large": 0.171,
}

# One-size-down rightsizing ladder (same family)
RIGHTSIZE_LADDER = {
    "m5.4xlarge": "m5.2xlarge",
    "m5.2xlarge": "m5.xlarge",
    "m5.xlarge": "m5.large",
    "m5.large": "t3.large",
    "c5.2xlarge": "c5.xlarge",
    "c5.xlarge": "c5.large",
    "t3.xlarge": "t3.large",
    "t3.large": "t3.medium",
}

# Graviton migration map (x86 -> Graviton, same vCPU count)
GRAVITON_MAP = {
    "m5.large": "m7g.large",
    "m5.xlarge": "m7g.xlarge",
    "m5.2xlarge": "m7g.2xlarge",
    "c5.xlarge": "c7g.xlarge",
}

SPOT_DISCOUNT = 0.62          # conservative realized spot savings vs on-demand
SAVINGS_PLAN_DISCOUNT = 0.27  # 1-yr Compute Savings Plans, conservative

# --- Storage ---------------------------------------------------------------
EBS_GB_MONTH = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "st1": 0.045, "sc1": 0.025}
SNAPSHOT_GB_MONTH = 0.05

# --- Networking ------------------------------------------------------------
EIP_IDLE_HOUR = 0.005
NAT_GATEWAY_HOUR = 0.045
NAT_PROCESSING_GB = 0.045
ALB_HOUR = 0.0225
ALB_LCU_HOUR = 0.008
DATA_TRANSFER_OUT_GB = 0.09

# --- S3 (us-west-2, first 50TB tier) ---------------------------------------
S3_GB_MONTH = {
    "standard": 0.023,
    "standard_ia": 0.0125,
    "glacier_ir": 0.004,
}
S3_LIFECYCLE_COLD_FRACTION = 0.60   # assumed share of objects -> IA/Glacier IR
S3_LIFECYCLE_SAVINGS_FRACTION = 0.45  # conservative blended savings on storage

# --- CloudWatch ------------------------------------------------------------
CW_LOGS_INGESTION_GB = 0.50
CW_LOGS_STORAGE_GB_MONTH = 0.03
CW_METRICS_MONTHLY_ESTIMATE = 5.0  # custom metrics + alarms allowance


def ec2_monthly(instance_type: str) -> float:
    return EC2_ONDEMAND[instance_type] * HOURS


def ebs_monthly(size_gb: int, volume_type: str) -> float:
    return size_gb * EBS_GB_MONTH.get(volume_type, 0.10)


def snapshot_monthly(size_gb: int) -> float:
    return size_gb * SNAPSHOT_GB_MONTH


def eip_idle_monthly() -> float:
    return EIP_IDLE_HOUR * HOURS


def nat_monthly() -> float:
    return NAT_GATEWAY_HOUR * HOURS


def alb_monthly(lcu_estimate: float = 1.0) -> float:
    return ALB_HOUR * HOURS + ALB_LCU_HOUR * HOURS * lcu_estimate


@dataclass
class PriceBook:
    """Snapshot of the pricing table (serialisable, served by the API)."""

    region: str = config.AWS_REGION
    ec2: dict = field(default_factory=lambda: dict(EC2_ONDEMAND))
    ebs: dict = field(default_factory=lambda: dict(EBS_GB_MONTH))
    snapshot_gb_month: float = SNAPSHOT_GB_MONTH
    eip_idle_month: float = EIP_IDLE_HOUR * HOURS
    nat_month: float = NAT_GATEWAY_HOUR * HOURS
    alb_month: float = ALB_HOUR * HOURS
    s3: dict = field(default_factory=lambda: dict(S3_GB_MONTH))
    hours_per_month: float = HOURS
    source: str = "AWS public on-demand list prices, us-west-2, 2024-12 (static table; refreshable via Pricing API in production)"

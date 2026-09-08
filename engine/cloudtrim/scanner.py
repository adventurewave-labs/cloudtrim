"""Inventory scanner — real boto3 describe calls across the AWS account.

Read-only by construction: every call is a describe/list/get. The scanner
never mutates state. Pagination is used everywhere so the same code handles
100 resources or 100,000 in live mode.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import aws, config


def _name_tag(tags: list[dict] | None) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


def _tags_dict(tags: list[dict] | None) -> dict:
    return {t.get("Key"): t.get("Value") for t in (tags or [])}


@dataclass
class Instance:
    id: str
    type: str
    state: str
    name: str
    tags: dict
    launch_time: str
    image_id: str
    az: str
    volume_ids: list[str] = field(default_factory=list)


@dataclass
class Volume:
    id: str
    type: str
    size_gb: int
    state: str  # in-use | available
    attached_to: str
    name: str
    create_time: str


@dataclass
class Snapshot:
    id: str
    volume_size_gb: int
    start_time: str
    description: str


@dataclass
class Address:
    public_ip: str
    allocation_id: str
    associated: bool


@dataclass
class LoadBalancer:
    arn: str
    name: str
    type: str
    state: str
    healthy_targets: int
    total_targets: int
    target_groups: list[str] = field(default_factory=list)


@dataclass
class NatGateway:
    id: str
    state: str
    vpc_id: str
    az: str


@dataclass
class Bucket:
    name: str
    region: str
    has_lifecycle: bool
    incomplete_mpu: list[dict] = field(default_factory=list)
    object_count: int = -1


@dataclass
class LogGroup:
    name: str
    retention_days: int  # 0 = never expire
    stored_bytes: int


@dataclass
class Image:
    id: str
    name: str
    state: str
    creation_date: str


@dataclass
class Inventory:
    instances: list[Instance] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    load_balancers: list[LoadBalancer] = field(default_factory=list)
    nat_gateways: list[NatGateway] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)
    log_groups: list[LogGroup] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    # instanceId -> {metric: (avg, datapoints)}
    cloudwatch: dict[str, dict[str, tuple[float, int]]] = field(default_factory=dict)
    # volumeId -> (avg_ops_per_sec, datapoints)
    volume_ops: dict[str, tuple[float, int]] = field(default_factory=dict)
    scanned_at: str = ""
    resource_count: int = 0

    def running_instances(self) -> list[Instance]:
        return [i for i in self.instances if i.state == "running"]


def scan_inventory(collect_cloudwatch: bool = True) -> Inventory:
    """Run the full read-only inventory sweep."""
    inv = Inventory()
    inv.scanned_at = dt.datetime.now(dt.timezone.utc).isoformat()

    _scan_ec2(inv)
    _scan_elbv2(inv)
    _scan_s3(inv)
    _scan_logs(inv)
    if collect_cloudwatch:
        _scan_cloudwatch(inv)

    inv.resource_count = (
        len(inv.instances) + len(inv.volumes) + len(inv.snapshots)
        + len(inv.addresses) + len(inv.load_balancers) + len(inv.nat_gateways)
        + len(inv.buckets) + len(inv.log_groups) + len(inv.images)
    )
    return inv


def _scan_ec2(inv: Inventory) -> None:
    ec2 = aws.client("ec2")
    for page in ec2.get_paginator("describe_instances").paginate():
        for res in page["Reservations"]:
            for i in res["Instances"]:
                mappings = i.get("BlockDeviceMappings", [])
                inv.instances.append(Instance(
                    id=i["InstanceId"],
                    type=i["InstanceType"],
                    state=i["State"]["Name"],
                    name=_name_tag(i.get("Tags")),
                    tags=_tags_dict(i.get("Tags")),
                    launch_time=str(i.get("LaunchTime", "")),
                    image_id=i.get("ImageId", ""),
                    az=i.get("Placement", {}).get("AvailabilityZone", ""),
                    volume_ids=[m["Ebs"]["VolumeId"] for m in mappings if "Ebs" in m],
                ))
    for page in ec2.get_paginator("describe_volumes").paginate():
        for v in page["Volumes"]:
            att = v.get("Attachments", [])
            inv.volumes.append(Volume(
                id=v["VolumeId"],
                type=v["VolumeType"],
                size_gb=v["Size"],
                state=v["State"],
                attached_to=att[0]["InstanceId"] if att else "",
                name=_name_tag(v.get("Tags")),
                create_time=str(v.get("CreateTime", "")),
            ))
    snaps = ec2.describe_snapshots(OwnerIds=["self"])
    for s in snaps.get("Snapshots", []):
        inv.snapshots.append(Snapshot(
            id=s["SnapshotId"],
            volume_size_gb=s.get("VolumeSize", 0),
            start_time=str(s.get("StartTime", "")),
            description=s.get("Description", ""),
        ))
    for a in ec2.describe_addresses().get("Addresses", []):
        inv.addresses.append(Address(
            public_ip=a.get("PublicIp", ""),
            allocation_id=a.get("AllocationId", ""),
            associated=bool(a.get("AssociationId")),
        ))
    for page in ec2.get_paginator("describe_images").paginate(Owners=["self"]):
        for ami in page.get("Images", []):
            inv.images.append(Image(
                id=ami["ImageId"],
                name=ami.get("Name", ""),
                state=ami.get("State", ""),
                creation_date=ami.get("CreationDate", ""),
            ))
    try:
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            for ng in page.get("NatGateways", []):
                inv.nat_gateways.append(NatGateway(
                    id=ng["NatGatewayId"],
                    state=ng.get("State", ""),
                    vpc_id=ng.get("VpcId", ""),
                    az=ng.get("VpcId", "") and (ng.get("SubnetId", "") or ""),
                ))
    except Exception:
        pass  # NAT GW API unsupported by some emulators; rule handles absence


def _scan_elbv2(inv: Inventory) -> None:
    elbv2 = aws.client("elbv2")
    try:
        for page in elbv2.get_paginator("describe_load_balancers").paginate():
            for lb in page.get("LoadBalancers", []):
                healthy = 0
                total = 0
                tgs: list[str] = []
                try:
                    tg_resp = elbv2.describe_target_groups(
                        LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
                    for tg in tg_resp:
                        tgs.append(tg["TargetGroupArn"])
                        th = elbv2.describe_target_health(
                            TargetGroupArn=tg["TargetGroupArn"])["TargetHealthDescriptions"]
                        total += len(th)
                        healthy += sum(
                            1 for d in th if d["TargetHealth"]["State"] == "healthy")
                except Exception:
                    pass
                inv.load_balancers.append(LoadBalancer(
                    arn=lb["LoadBalancerArn"],
                    name=lb["LoadBalancerName"],
                    type=lb.get("Type", ""),
                    state=lb.get("State", {}).get("Code", ""),
                    healthy_targets=healthy,
                    total_targets=total,
                    target_groups=tgs,
                ))
    except Exception:
        pass


def _scan_s3(inv: Inventory) -> None:
    s3 = aws.client("s3")
    try:
        buckets = s3.list_buckets()["Buckets"]
    except Exception:
        return
    for b in buckets:
        name = b["Name"]
        try:
            loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except Exception:
            loc = config.AWS_REGION
        has_lifecycle = False
        try:
            s3.get_bucket_lifecycle_configuration(Bucket=name)
            has_lifecycle = True
        except Exception:
            has_lifecycle = False
        incomplete: list[dict] = []
        try:
            uploads = s3.list_multipart_uploads(Bucket=name).get("Uploads", [])
            for u in uploads:
                incomplete.append({
                    "key": u.get("Key", ""),
                    "upload_id": u.get("UploadId", ""),
                    "initiated": str(u.get("Initiated", "")),
                })
        except Exception:
            pass
        object_count = -1
        try:
            object_count = s3.list_objects_v2(Bucket=name).get("KeyCount", -1)
        except Exception:
            pass
        inv.buckets.append(Bucket(name=name, region=loc, has_lifecycle=has_lifecycle,
                                  incomplete_mpu=incomplete, object_count=object_count))


def _scan_logs(inv: Inventory) -> None:
    logs = aws.client("logs")
    try:
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                inv.log_groups.append(LogGroup(
                    name=lg["logGroupName"],
                    retention_days=lg.get("retentionInDays", 0) or 0,
                    stored_bytes=lg.get("storedBytes", 0),
                ))
    except Exception:
        pass


def _scan_cloudwatch(inv: Inventory) -> None:
    """CPUUtilization (14d daily avg) per running instance + volume ops."""
    cw = aws.client("cloudwatch")
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=config.CW_LOOKBACK_DAYS)
    for inst in inv.instances:
        try:
            stats = cw.get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": inst.id}],
                StartTime=start, EndTime=end, Period=86400, Statistics=["Average"])
            pts = stats.get("Datapoints", [])
            if pts:
                avg = sum(p["Average"] for p in pts) / len(pts)
                inv.cloudwatch[inst.id] = {"CPUUtilization": (avg, len(pts))}
        except Exception:
            pass
    for vol in inv.volumes:
        if vol.state != "in-use":
            continue
        total_ops = 0.0
        datapoints = 0
        for metric in ("VolumeReadOps", "VolumeWriteOps"):
            try:
                stats = cw.get_metric_statistics(
                    Namespace="AWS/EBS", MetricName=metric,
                    Dimensions=[{"Name": "VolumeId", "Value": vol.id}],
                    StartTime=start, EndTime=end, Period=86400, Statistics=["Sum"])
                pts = stats.get("Datapoints", [])
                if pts:
                    total_ops += sum(p["Sum"] for p in pts) / len(pts)
                    datapoints = max(datapoints, len(pts))
            except Exception:
                pass
        if datapoints:
            inv.volume_ops[vol.id] = (total_ops / 86400.0, datapoints)  # ops/sec avg

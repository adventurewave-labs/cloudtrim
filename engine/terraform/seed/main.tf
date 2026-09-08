# CloudTrim demo seed — a realistic, deliberately-wasteful startup AWS account
#
# This is REAL Terraform: `terraform apply` here creates the same
# infrastructure against LocalStack (docker-compose), moto (CI), or a real
# AWS scratch account (empty var.aws_endpoint). The seeded account models
# an 18-month-old LA startup: production fleet + worker fleet + staging/
# sandbox sprawl + the classic cost waste (idle instances, orphaned
# volumes, an idle ALB, S3 buckets with no lifecycle, unbounded logs).
#
# NOTE: instances intentionally use default root volumes + separate
# aws_ebs_volume/aws_volume_attachment resources (the inline
# ebs_block_device path relies on EC2 filters some emulators don't
# implement; the decoupled pattern is also the reviewed-IaC best practice
# for adoptable volumes).
#
# Region: us-west-2 (home region for LA startups).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_endpoint" {
  description = "AWS-compatible API endpoint (LocalStack/moto). Empty string = real AWS."
  type        = string
  default     = "http://localstack:4566"
}

variable "prod_count" {
  type    = number
  default = 10
}

variable "worker_count" {
  type    = number
  default = 6
}

variable "staging_count" {
  type    = number
  default = 5
}

variable "sandbox_count" {
  type    = number
  default = 3
}

variable "dev_count" {
  type    = number
  default = 2
}

provider "aws" {
  region                      = "us-west-2"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  skip_region_validation      = true

  dynamic "endpoints" {
    for_each = var.aws_endpoint == "" ? [] : [1]
    content {
      ec2        = var.aws_endpoint
      elbv2      = var.aws_endpoint
      s3         = var.aws_endpoint
      logs       = var.aws_endpoint
      sts        = var.aws_endpoint
      cloudwatch = var.aws_endpoint
    }
  }
}

locals {
  azs = ["us-west-2a", "us-west-2b", "us-west-2c"]
}

# ---------------------------------------------------------------------------
# Network: one VPC, three public subnets
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.72.0.0/16"
  enable_dns_hostnames = true
  tags = { Name = "cloudtrim-demo-vpc", env = "prod" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags = { Name = "cloudtrim-demo-igw" }
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.72.${count.index + 1}.0/24"
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = { Name = "cloudtrim-demo-subnet-${count.index}", env = "prod" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "cloudtrim-demo-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# EC2 fleets. Instances get default 8 GB gp2 root volumes (as most real
# accounts do); data volumes are separate, adoptable resources below.
# ---------------------------------------------------------------------------

resource "aws_instance" "prod" {
  count         = var.prod_count
  ami           = "ami-0123456789abcdef0"
  instance_type = "m5.2xlarge"
  subnet_id     = aws_subnet.public[count.index % 3].id
  tags = {
    Name     = "prod-api-${count.index}"
    env      = "prod"
    workload = "service"
  }
}

resource "aws_ebs_volume" "prod_data" {
  count             = var.prod_count
  availability_zone = local.azs[count.index % 3]
  size              = 500
  type              = "gp2"
  tags = { Name = "prod-api-${count.index}-data", env = "prod" }
}

resource "aws_volume_attachment" "prod_data" {
  count       = var.prod_count
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.prod_data[count.index].id
  instance_id = aws_instance.prod[count.index].id
}

# Stateless worker fleet (interruption-tolerant -> spot migration candidate)
resource "aws_instance" "worker" {
  count         = var.worker_count
  ami           = "ami-0123456789abcdef0"
  instance_type = "m5.2xlarge"
  subnet_id     = aws_subnet.public[count.index % 3].id
  tags = {
    Name     = "prod-worker-${count.index}"
    env      = "prod"
    workload = "stateless"
  }
}

resource "aws_ebs_volume" "worker_data" {
  count             = var.worker_count
  availability_zone = local.azs[count.index % 3]
  size              = 200
  type              = "gp2"
  tags = { Name = "prod-worker-${count.index}-data", env = "prod" }
}

resource "aws_volume_attachment" "worker_data" {
  count       = var.worker_count
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.worker_data[count.index].id
  instance_id = aws_instance.worker[count.index].id
}

# Staging sprawl (~3% CPU — idle)
resource "aws_instance" "staging" {
  count         = var.staging_count
  ami           = "ami-0123456789abcdef0"
  instance_type = "m5.2xlarge"
  subnet_id     = aws_subnet.public[count.index % 3].id
  tags = {
    Name     = "staging-api-${count.index}"
    env      = "staging"
    workload = "service"
  }
}

resource "aws_ebs_volume" "staging_data" {
  count             = var.staging_count
  availability_zone = local.azs[count.index % 3]
  size              = 100
  type              = "gp2"
  tags = { Name = "staging-api-${count.index}-data", env = "staging" }
}

resource "aws_volume_attachment" "staging_data" {
  count       = var.staging_count
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.staging_data[count.index].id
  instance_id = aws_instance.staging[count.index].id
}

# Sandbox experiments nobody shut down (~2% CPU)
resource "aws_instance" "sandbox" {
  count         = var.sandbox_count
  ami           = "ami-0123456789abcdef0"
  instance_type = "m5.2xlarge"
  subnet_id     = aws_subnet.public[count.index % 3].id
  tags = {
    Name     = "sandbox-${count.index}"
    env      = "dev"
    workload = "sandbox"
  }
}

resource "aws_ebs_volume" "sandbox_data" {
  count             = var.sandbox_count
  availability_zone = local.azs[count.index % 3]
  size              = 100
  type              = "gp2"
  tags = { Name = "sandbox-${count.index}-data", env = "dev" }
}

resource "aws_volume_attachment" "sandbox_data" {
  count       = var.sandbox_count
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.sandbox_data[count.index].id
  instance_id = aws_instance.sandbox[count.index].id
}

# Dev boxes (~9.5% CPU — rightsizing candidates)
resource "aws_instance" "dev" {
  count         = var.dev_count
  ami           = "ami-0123456789abcdef0"
  instance_type = "m5.xlarge"
  subnet_id     = aws_subnet.public[count.index % 3].id
  tags = {
    Name     = "dev-box-${count.index}"
    env      = "dev"
    workload = "dev"
  }
}

resource "aws_ebs_volume" "dev_data" {
  count             = var.dev_count
  availability_zone = local.azs[count.index % 3]
  size              = 50
  type              = "gp3"
  tags = { Name = "dev-box-${count.index}-home", env = "dev" }
}

resource "aws_volume_attachment" "dev_data" {
  count       = var.dev_count
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.dev_data[count.index].id
  instance_id = aws_instance.dev[count.index].id
}

# ---------------------------------------------------------------------------
# Orphaned EBS volumes (unattached = pure waste)
# ---------------------------------------------------------------------------

resource "aws_ebs_volume" "orphan" {
  count             = 4
  availability_zone = "us-west-2a"
  size              = 200
  type              = "gp2"
  tags = { Name = "etl-temp-${count.index}", env = "dev" }
}

resource "aws_ebs_volume" "orphan_warehouse" {
  availability_zone = "us-west-2a"
  size              = 1000
  type              = "gp2"
  tags = { Name = "orphaned-etl-warehouse", env = "dev" }
}

# ---------------------------------------------------------------------------
# Orphaned Elastic IPs (billed while unassociated)
# ---------------------------------------------------------------------------

resource "aws_eip" "orphan" {
  count  = 3
  domain = "vpc"
  tags   = { Name = "legacy-ip-${count.index}" }
}

# ---------------------------------------------------------------------------
# Load balancers: one live, one abandoned
# ---------------------------------------------------------------------------

resource "aws_lb" "prod_web" {
  name               = "prod-web-alb"
  internal           = false
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  tags = { Name = "prod-web-alb", env = "prod" }
}

resource "aws_lb_target_group" "prod_web" {
  name     = "prod-web-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id
  tags = { Name = "prod-web-tg", env = "prod" }
}

resource "aws_lb_target_group_attachment" "prod_web" {
  count            = 2
  target_group_arn = aws_lb_target_group.prod_web.arn
  target_id        = aws_instance.prod[count.index].id
  port             = 80
}

resource "aws_lb_listener" "prod_web" {
  load_balancer_arn = aws_lb.prod_web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.prod_web.arn
  }
}

# The abandoned one: internal ALB with an EMPTY target group
resource "aws_lb" "legacy" {
  name               = "legacy-internal-alb"
  internal           = true
  load_balancer_type = "application"
  subnets            = [aws_subnet.public[0].id, aws_subnet.public[1].id]
  tags = { Name = "legacy-internal-alb", env = "dev" }
}

resource "aws_lb_target_group" "legacy" {
  name     = "legacy-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id
  tags = { Name = "legacy-tg", env = "dev" }
}

# ---------------------------------------------------------------------------
# S3: buckets without lifecycle policies (the classic cold-data tax)
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "assets" {
  bucket = "cloudtrim-demo-assets"
  tags = { Name = "cloudtrim-demo-assets", env = "prod" }
}

resource "aws_s3_bucket" "backups" {
  bucket = "cloudtrim-demo-backups"
  tags = { Name = "cloudtrim-demo-backups", env = "prod" }
}

resource "aws_s3_bucket" "ml_data" {
  bucket = "cloudtrim-demo-ml-data"
  tags = { Name = "cloudtrim-demo-ml-data", env = "dev" }
}

# The one bucket that already does it right (lifecycle applied)
resource "aws_s3_bucket" "logs_archive" {
  bucket = "cloudtrim-demo-logs-archive"
  tags = { Name = "cloudtrim-demo-logs-archive", env = "prod" }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs_archive" {
  bucket = aws_s3_bucket.logs_archive.id

  rule {
    id     = "archive-coldening"
    status = "Enabled"
    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------
# CloudWatch Logs: one bounded group, two unbounded ones
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "prod_api" {
  name = "/ecs/prod-api"
  # retention_in_days intentionally omitted -> Never Expire
  tags = { env = "prod" }
}

resource "aws_cloudwatch_log_group" "staging" {
  name = "/ecs/staging"
  # retention_in_days intentionally omitted -> Never Expire
  tags = { env = "staging" }
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/demo-api"
  retention_in_days = 90
  tags = { env = "prod" }
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "instance_ids" {
  value = concat(aws_instance.prod[*].id, aws_instance.worker[*].id,
    aws_instance.staging[*].id, aws_instance.sandbox[*].id, aws_instance.dev[*].id)
}

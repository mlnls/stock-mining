# Stock-Mining on AWS (Seoul): 1× EC2 t4g.small + S3 (releases/backups) + SSM (secrets & shell access).
# No inbound ports: Telegram uses long polling, admin access is via SSM Session Manager.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "ap-northeast-2" # Seoul: KRX/Naver data sources can be flaky from overseas IPs
}

variable "instance_type" {
  type    = string
  default = "t4g.small" # 2 vCPU / 2 GB (Graviton). t4g.micro (1 GB) is too small for the full backtest panel.
}

variable "name" {
  type    = string
  default = "stockmining"
}

data "aws_caller_identity" "me" {}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
}

# ---------------------------------------------------------------- S3: releases + DB backups
resource "aws_s3_bucket" "this" {
  bucket = "${var.name}-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    id     = "expire-old-backup-versions"
    status = "Enabled"
    filter { prefix = "backups/" }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# ---------------------------------------------------------------- IAM
resource "aws_iam_role" "ec2" {
  name = "${var.name}-ec2"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "app" {
  name = "${var.name}-app"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter/${var.name}/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.name}-ec2"
  role = aws_iam_role.ec2.name
}

# ---------------------------------------------------------------- network (default VPC, egress only)
resource "aws_security_group" "this" {
  name        = "${var.name}-egress-only"
  description = "No inbound. Outbound for market data, Anthropic API, Telegram, S3, SSM."
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------- EC2
resource "aws_instance" "this" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  vpc_security_group_ids = [aws_security_group.this.id]

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -eux
    timedatectl set-timezone Asia/Seoul
    apt-get update
    apt-get install -y python3-venv python3-pip sqlite3 curl unzip jq
    snap install aws-cli --classic || true
    useradd --system --home /var/lib/stockmining --create-home --shell /usr/sbin/nologin stockmining || true
    echo "${aws_s3_bucket.this.bucket}" > /etc/stockmining-bucket
  EOF

  tags = { Name = var.name }
}

output "instance_id" { value = aws_instance.this.id }
output "bucket" { value = aws_s3_bucket.this.bucket }
output "region" { value = var.region }

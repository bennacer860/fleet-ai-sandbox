terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ------------------------------------------------------------------------------
# VPC & Networking
# ------------------------------------------------------------------------------

resource "aws_vpc" "bot_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "polymarket-bot-vpc"
  }
}

resource "aws_internet_gateway" "bot_igw" {
  vpc_id = aws_vpc.bot_vpc.id

  tags = {
    Name = "polymarket-bot-igw"
  }
}

resource "aws_subnet" "bot_subnet" {
  vpc_id                  = aws_vpc.bot_vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${var.aws_region}a"

  tags = {
    Name = "polymarket-bot-subnet"
  }
}

resource "aws_route_table" "bot_rt" {
  vpc_id = aws_vpc.bot_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.bot_igw.id
  }

  tags = {
    Name = "polymarket-bot-rt"
  }
}

resource "aws_route_table_association" "bot_rta" {
  subnet_id      = aws_subnet.bot_subnet.id
  route_table_id = aws_route_table.bot_rt.id
}

# ------------------------------------------------------------------------------
# Security Group (No Inbound Rules)
# ------------------------------------------------------------------------------

resource "aws_security_group" "bot_sg" {
  name        = "polymarket-bot-sg"
  description = "Security group for Polymarket bot (outbound only)"
  vpc_id      = aws_vpc.bot_vpc.id

  # No ingress rules - access via SSM Session Manager only

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  tags = {
    Name = "polymarket-bot-sg"
  }
}

# ------------------------------------------------------------------------------
# S3 Bucket for Litestream Backups
# ------------------------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "bot_backups" {
  bucket = "polymarket-bot-backups-${random_id.bucket_suffix.hex}"
}

resource "aws_s3_bucket_public_access_block" "bot_backups_block" {
  bucket = aws_s3_bucket.bot_backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------
# IAM Role & Instance Profile
# ------------------------------------------------------------------------------

resource "aws_iam_role" "bot_role" {
  name = "polymarket-bot-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })
}

# Managed policy for SSM Session Manager
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.bot_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Custom policy for S3 and Parameter Store access
resource "aws_iam_role_policy" "bot_policy" {
  name = "polymarket-bot-policy"
  role = aws_iam_role.bot_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParametersByPath",
          "ssm:GetParameters",
          "ssm:GetParameter"
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_parameter_prefix}*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.bot_backups.arn,
          "${aws_s3_bucket.bot_backups.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "bot_profile" {
  name = "polymarket-bot-profile"
  role = aws_iam_role.bot_role.name
}

data "aws_caller_identity" "current" {}

# ------------------------------------------------------------------------------
# EC2 Instance
# ------------------------------------------------------------------------------

# Amazon Linux 2023 ARM64 AMI
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
}

resource "aws_instance" "bot_instance" {
  ami                  = data.aws_ami.al2023_arm64.id
  instance_type        = var.instance_type
  subnet_id            = aws_subnet.bot_subnet.id
  iam_instance_profile = aws_iam_instance_profile.bot_profile.name
  vpc_security_group_ids = [aws_security_group.bot_sg.id]

  # Optional Spot Instance configuration
  dynamic "instance_market_options" {
    for_each = var.use_spot_instance ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type = "one-time"
      }
    }
  }

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    ssm_prefix = var.ssm_parameter_prefix
    region     = var.aws_region
    s3_bucket  = aws_s3_bucket.bot_backups.id
  })

  tags = {
    Name = "polymarket-bot"
  }
}

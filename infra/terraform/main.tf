# Terraform for one cloud (AWS), written so the second cloud is a provider swap rather than a
# rewrite. The brief asks for deployment on at least one cloud "with no code changes needed for
# another", and the way that promise is kept is visible here:
#
#   * The application runs on plain Kubernetes. Nothing in the app talks to an AWS API.
#   * No managed service sits on the critical path. Postgres, ClickHouse, Redpanda and Redis are
#     addressed by endpoint, injected as environment variables.
#   * Everything AWS-specific is in this directory. Porting to GKE or AKS means writing a
#     sibling directory with the same four outputs; the Helm chart and the images do not change.
#
# The temptation is to use RDS, MSK and ElastiCache. Each would be operationally better and each
# would weld us to AWS -- which is exactly the lock-in the brief calls out (AWS IoT FleetWise
# closing to new customers in April 2026). We take the operational cost deliberately.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.60" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.31" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type        = string
  default     = "ap-south-1"
  description = "Mumbai: telemetry from an Indian fleet should not leave the country, and DPDP makes that a compliance point rather than a latency preference."
}

variable "cluster_name" {
  type    = string
  default = "readyline"
}

variable "node_instance_type" {
  type        = string
  default     = "m6i.xlarge"
  description = "4 vCPU / 16 GiB. Sized from the measured 6,050 ev/s per ingest consumer (docs/BENCHMARKS.md), not guessed."
}

variable "node_count" {
  type        = number
  default     = 6
  description = "17 ingest consumers at ~0.5 vCPU plus 3 API replicas plus overhead fits in 6 nodes with headroom for a 3x burst."
}

# ----------------------------------------------------------------- network

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.cluster_name}-vpc"
  cidr = "10.42.0.0/16"

  azs             = ["${var.region}a", "${var.region}b", "${var.region}c"]
  private_subnets = ["10.42.1.0/24", "10.42.2.0/24", "10.42.3.0/24"]
  public_subnets  = ["10.42.101.0/24", "10.42.102.0/24", "10.42.103.0/24"]

  # Three AZs, because the brief asks for no single point of failure and a single-AZ cluster
  # fails that on the first AZ event.
  enable_nat_gateway = true
  single_nat_gateway = false
  enable_dns_hostnames = true

  tags = local.tags
}

# ----------------------------------------------------------------- cluster

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name    = var.cluster_name
  cluster_version = "1.30"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Private API endpoint. A public control plane is an unnecessary attack surface for a platform
  # holding vehicle location data.
  cluster_endpoint_public_access  = false
  cluster_endpoint_private_access = true

  cluster_encryption_config = {
    resources = ["secrets"]
  }

  eks_managed_node_groups = {
    workers = {
      instance_types = [var.node_instance_type]
      min_size       = 3
      max_size       = 20
      desired_size   = var.node_count
      # Nodes in private subnets only; ingress arrives through the load balancer.
      subnet_ids = module.vpc.private_subnets
    }
  }

  tags = local.tags
}

# ----------------------------------------------------------------- cold tier

resource "aws_s3_bucket" "cold" {
  bucket = "${var.cluster_name}-telemetry-cold"
  tags   = local.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cold" {
  bucket = aws_s3_bucket.cold.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cold" {
  bucket                  = aws_s3_bucket.cold.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "cold" {
  bucket = aws_s3_bucket.cold.id

  # Mirrors the hot/warm/cold tiering in docs/ARCHITECTURE.md. Seven years is the retention the
  # compliance story commits to; beyond that the data is deleted, not archived, because holding
  # location history longer than we can justify is a DPDP liability rather than an asset.
  rule {
    id     = "telemetry-tiering"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 365
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = 2555
    }
  }
}

locals {
  tags = {
    Project    = "readyline"
    ManagedBy  = "terraform"
    Compliance = "dpdp-gdpr"
  }
}

output "cluster_endpoint" { value = module.eks.cluster_endpoint }
output "cluster_name"     { value = module.eks.cluster_name }
output "cold_bucket"      { value = aws_s3_bucket.cold.id }
output "region"           { value = var.region }

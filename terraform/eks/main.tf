terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  cluster_name = var.cluster_name

  # Resolve cluster endpoint and CA regardless of create vs existing
  cluster_endpoint = var.create_cluster ? aws_eks_cluster.this[0].endpoint : data.aws_eks_cluster.existing[0].endpoint
  cluster_ca       = var.create_cluster ? aws_eks_cluster.this[0].certificate_authority[0].data : data.aws_eks_cluster.existing[0].certificate_authority[0].data

  # OIDC issuer URL (without https://)
  oidc_issuer = var.create_cluster ? replace(aws_eks_cluster.this[0].identity[0].oidc[0].issuer, "https://", "") : replace(data.aws_eks_cluster.existing[0].identity[0].oidc[0].issuer, "https://", "")

  account_id = data.aws_caller_identity.current.account_id

  ecr_components = [
    "opensandbox/server",
    "opensandbox/controller",
    "opensandbox/execd",
    "opensandbox/egress-sidecar",
  ]

  common_tags = {
    Project   = "opensandbox"
    ManagedBy = "terraform"
  }
}

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Optional: uncomment to store state in S3 instead of locally.
  # Create the bucket manually first: aws s3 mb s3://penumbra-tfstate-<account-id>
  #
  # backend "s3" {
  #   bucket = "penumbra-tfstate-<account-id>"
  #   key    = "penumbra/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region
}

# ── Caller identity (used in OIDC trust policy) ────────────────────────────

data "aws_caller_identity" "current" {}

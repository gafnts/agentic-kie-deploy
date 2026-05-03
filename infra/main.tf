provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket_suffix = substr(sha256("${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"), 0, 8)
  bucket_name   = "${var.project_name}-${var.environment}-ingestion-${local.bucket_suffix}"
}

module "storage" {
  source                 = "./modules/storage"
  bucket_name            = local.bucket_name
  allowed_upload_origins = var.allowed_upload_origins
  force_destroy          = var.environment != "prod"
}

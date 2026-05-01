provider "aws" {
  region = var.aws_region
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  ingestion_bucket_name = "${var.project_name}-${var.environment}-ingestion-${random_id.bucket_suffix.hex}"
}

resource "aws_s3_bucket" "ingestion_bucket" {
  bucket = local.ingestion_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "ingestion_bucket" {
  bucket = aws_s3_bucket.ingestion_bucket.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ingestion_bucket" {
  bucket = aws_s3_bucket.ingestion_bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_notification" "ingestion_bucket" {
  bucket      = aws_s3_bucket.ingestion_bucket.id
  eventbridge = true
}

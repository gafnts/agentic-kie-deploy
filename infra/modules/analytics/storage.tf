resource "aws_s3_bucket" "results" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = {
    Environment = var.environment
  }
}

resource "aws_s3_bucket_ownership_controls" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id

  versioning_configuration {
    status = "Enabled"
  }
}

#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_logging" "results" {
  bucket        = aws_s3_bucket.results.id
  target_bucket = aws_s3_bucket.results_logs.id
  target_prefix = "access-logs/"
}

resource "aws_s3_bucket_notification" "results" {
  bucket      = aws_s3_bucket.results.id
  eventbridge = true
}

# Results stay in STANDARD: Athena queries the same objects on demand and cannot
# transparently restore from Glacier, so no cold-tier transition is configured.
# No current-version expiration at MVP. Results are the durable record of work.
resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-noncurrent-and-abort-multipart"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "results_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.results.arn,
      "${aws_s3_bucket.results.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "results_tls_only" {
  bucket = aws_s3_bucket.results.id
  policy = data.aws_iam_policy_document.results_tls_only.json
}

#trivy:ignore:AVD-AWS-0089
#trivy:ignore:AVD-AWS-0090
resource "aws_s3_bucket" "results_logs" {
  bucket        = "${var.bucket_name}-logs"
  force_destroy = var.force_destroy

  tags = {
    Environment = var.environment
  }
}

resource "aws_s3_bucket_ownership_controls" "results_logs" {
  bucket = aws_s3_bucket.results_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "results_logs" {
  bucket = aws_s3_bucket.results_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "results_logs" {
  bucket = aws_s3_bucket.results_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "results_logs" {
  bucket = aws_s3_bucket.results_logs.id

  rule {
    id     = "expire-logs"
    status = "Enabled"

    expiration {
      days = var.access_log_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

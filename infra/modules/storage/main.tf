resource "aws_s3_bucket" "ingestion" {
  bucket        = var.bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "ingestion" {
  bucket = aws_s3_bucket.ingestion.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ingestion" {
  bucket = aws_s3_bucket.ingestion.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_notification" "ingestion" {
  bucket      = aws_s3_bucket.ingestion.id
  eventbridge = true
}

data "aws_iam_policy_document" "ingestion_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.ingestion.arn,
      "${aws_s3_bucket.ingestion.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "ingestion_tls_only" {
  bucket = aws_s3_bucket.ingestion.id
  policy = data.aws_iam_policy_document.ingestion_tls_only.json
}

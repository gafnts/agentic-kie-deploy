# ADR-0004: Use SSE-S3 Over SSE-KMS for Ingestion Bucket Encryption

## Status

Accepted (2026-05-01)

## Context

ADR-0003 established default encryption as one of the four bucket hardening layers. The remaining decision is which encryption strategy to use. Two managed options exist:

- **SSE-S3 (AES256)** — AWS manages the key transparently. No per-call KMS charges, no additional IAM grants, no encryption headers on pre-signed PUTs.
- **SSE-KMS with a customer-managed key (CMK)** — adds a second, independent permission gate (`kms:Decrypt` in addition to `s3:GetObject`), full CloudTrail auditability on every decrypt, and a kill switch (disabling the key makes every encrypted object immediately inaccessible). Cost concern is largely neutralized by S3 Bucket Keys, which cache a data key per bucket partition for ~24 hours and reduce KMS API calls by roughly 99%.

The AWS-managed KMS key (`aws/s3`) is a third option but a strictly dominated one: it incurs KMS operational cost without giving control over the key policy or the ability to disable it.

This is a portfolio project. No real PII or regulated documents will be uploaded.

## Decision

Use SSE-S3 (AES256). The AWS-managed KMS key (`aws/s3`) is not considered.

If this project moves beyond the portfolio stage and begins ingesting real documents, switch to SSE-KMS with a CMK and Bucket Keys enabled before any real data arrives. Changing default encryption only affects new objects; existing objects must be re-encrypted via a copy-in-place job, so the cheapest moment to switch is before real documents land.

The shape of the change when the time comes:

```hcl
resource "aws_kms_key" "ingestion" {
  description             = "Encrypts documents in the ingestion bucket"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "ingestion" {
  name          = "alias/${var.project_name}-${var.environment}-ingestion"
  target_key_id = aws_kms_key.ingestion.key_id
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ingestion" {
  bucket = aws_s3_bucket.ingestion.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.ingestion.arn
    }
    bucket_key_enabled = true
  }
}
```

The extractor Lambda's IAM policy will also need `kms:Decrypt` and `kms:GenerateDataKey` on the key. The presigner does not — it never reads encrypted bytes, only signs URLs.

## Consequences

Positive:
- No KMS per-call charges, no additional IAM grants, no encryption headers on pre-signed PUTs
- Zero operational overhead; appropriate for a portfolio project with no real data

Negative:
- No second permission gate: a principal with `s3:GetObject` can read objects without a separate `kms:Decrypt` check
- No CloudTrail auditability on decrypts
- No kill switch: objects remain accessible as long as AWS manages the key

Neutral:
- The migration to SSE-KMS only touches new objects; timing the switch before real data arrives eliminates the copy-in-place cost entirely

## Alternatives considered

- **AWS-managed KMS key (`aws/s3`)**: rejected — incurs KMS operational cost (per-call charges, IAM grants, encryption headers on pre-signed PUTs) without granting control over the key policy or the ability to disable it. Strictly worse than both SSE-S3 and a CMK.
- **SSE-KMS with a CMK now**: deferred — the correct posture for a production workload ingesting PII or regulated documents, but disproportionate for a portfolio project with no real data. The switch should happen at the boundary where real documents begin arriving.

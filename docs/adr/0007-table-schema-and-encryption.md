# ADR-0007: Table Schema, Bounded Item Size, and Encryption Posture

## Status

Accepted (2026-05-08)

## Context

The `table` module is the system of record for extraction results. Four decisions shape it: the key schema, what is allowed to live inside an item, the encryption strategy, and whether to enable Streams. They are coupled enough that one ADR is clearer than four.

### Key schema

`document_id` (UUIDv7, fixed in ADR-0006) is the only identifier callers ever need. There is at most one canonical extraction per document, and the polling endpoint reads by `GetItem(document_id)`. A sort key would only earn its place if we kept extraction history (e.g. one row per re-run), and that is not a current requirement.

The PK choice is also load-bearing for idempotency. Because `document_id` is minted once at presign (ADR-0006), every SQS redelivery of the same upload event resolves to the same partition key. Without that, no amount of conditional writes in the extractor would help—retries would simply land on different rows.

### What goes inside an item

`agentic-kie` today produces small, bounded JSON—a handful of fields, sub-entities, optional nulls. A realistic near-term expansion adds per-field confidence scores and processing metadata (model version, token usage, timing, status). All of that is bounded by *schema complexity*, which is developer-controlled and stays well under 10 KB.

Two payloads scale with *document length* rather than schema, and they are the only things that can blow DynamoDB's 400 KB item cap:

- **The OCR'd document text.** A receipt is ~1 KB; a long agreement is hundreds of KB. Even when it fits, every read of an "is it done yet?" poll pulls the full text over the wire, and DynamoDB RCU rounds up in 4 KB blocks—a 200 KB item costs ~50× more to read than a 4 KB one.
- **The agent trace.** Tool calls, intermediate states, retries, reasoning. Unbounded by nature, written once, read only when debugging.

Neither is part of the user-facing answer. The source document already lives in S3; the OCR'd text is a derived artifact that can be re-derived or cached as a sibling object next to the source. The agent trace is observability data, not persistence data, and belongs in an LLM-aware trace store (LangSmith at MVP; self-hosted Langfuse or Phoenix when real data arrives—settled in ADR-0009).

This means the table holds the *answer*, nothing else. Predictable single-digit-KB items, single-GetItem polling, and no dual-write coordination between DDB and S3 in the extractor's hot path.

### Encryption

ADR-0004 deferred CMKs for the ingestion bucket on the grounds that this is a portfolio project with no real PII. The same reasoning applies to the table—arguably more so, since the structured fields are exactly what would be PII in a production deployment (names, dates, jurisdictions).

DynamoDB offers three encryption-at-rest options:

- **AWS-owned key** (default, free, no audit trail, no key visibility).
- **AWS-managed KMS key** (`aws/dynamodb`)—free in DynamoDB's case (unlike S3, DynamoDB does not bill per-call KMS charges for the AWS-managed key), gives basic CloudTrail visibility on the encryption context.
- **Customer-managed KMS key (CMK)**—adds a second permission gate (`kms:Decrypt` on top of `dynamodb:GetItem`), full CloudTrail auditability, and a kill switch.

For parity with the storage module's posture, and to keep both data stores' encryption stories consistent, we use the AWS-managed KMS key in both environments. The migration to a CMK is the same shape as the one sketched in ADR-0004 and should happen at the same boundary: before real documents begin arriving.

### Streams

DynamoDB Streams enables change-driven downstream consumers (e.g. a webhook on completion, an analytics fan-out, a search index sync). There is no such consumer today. Enabling Streams is a non-breaking change—turning it on later does not require a table rebuild—so the cost of deferring is essentially zero.

## Decision

### Key schema

- Partition key: `document_id` (string, UUIDv7 per ADR-0006).
- No sort key.
- No GSIs at MVP. A sparse `status` GSI is the most likely future addition and can be added without rewriting items.

### Item shape (contract)

The table stores the answer and only the answer:

| Attribute         | Type   | Notes                                                    |
| ----------------- | ------ | -------------------------------------------------------- |
| `document_id`     | S (PK) | UUIDv7                                                   |
| `status`          | S      | `pending` \| `succeeded` \| `failed`                     |
| `created_at`      | S      | ISO-8601, set by extractor on first write                |
| `completed_at`    | S      | ISO-8601, set on terminal status                         |
| `extracted_fields`| M      | The structured answer map produced by `agentic-kie`      |
| `confidences`     | M      | Parallel map of per-field confidence scores (optional)   |
| `model_version`   | S      | Model identifier used for the extraction                 |
| `token_usage`     | M      | `{input, output}` integers (optional)                    |
| `processing_ms`   | N      | Wall-clock duration of the agent run (optional)          |
| `error`           | M      | `{code, message}`, present only on `status = failed`     |
| `ttl`             | N      | Epoch seconds; TTL enabled but unused at MVP             |

Explicitly **not** in the table:

- The OCR'd document text—derived from the S3 source, cached as a sibling S3 object if and when caching is justified.
- The agent trace—shipped to LangSmith via `@traceable` on the extractor's `extract()` wrapper (ADR-0009).
- The source document bytes—already in the ingestion bucket.

### Idempotency

Idempotency is a shared responsibility split across the schema and the extractor. The schema's half is ensuring retries land on the same row: `document_id` is minted once at presign (ADR-0006), so the partition key is stable across SQS redeliveries, and the absence of a sort key means there is exactly one row per document for a conditional write to target. The extractor's half is ensuring retries do not clobber later state: a conditional `PutItem` (`attribute_not_exists(document_id)`) on first write, and status-state-machine `UpdateItem` for transitions, so a redelivered message cannot overwrite a terminal `succeeded` or `failed` row. Both halves must hold; the table module's README documents the schema half and points at the extractor module for the operation half.

### Encryption

Enable server-side encryption with the AWS-managed KMS key (`aws/dynamodb`) in both `staging` and `prod`:

```hcl
resource "aws_dynamodb_table" "results" {
  name         = var.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
    # kms_key_arn omitted -> AWS-managed aws/dynamodb key
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  deletion_protection_enabled = var.environment == "prod"
}
```

PITR is enabled in both environments. The aim is to keep the table's configuration near-identical across `staging` and `prod`—divergence between environments is itself a source of surprise—and PITR's cost is negligible against a portfolio-scale workload. Deletion protection stays prod-only because it would block `terraform destroy`, which is part of the dev iteration loop (`make destroy`).

If this project moves beyond the portfolio stage and begins ingesting real PII, switch to a CMK before real data arrives. The shape of that change:

```hcl
resource "aws_kms_key" "results" {
  description             = "Encrypts extraction results in DynamoDB"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_dynamodb_table" "results" {
  # ...
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.results.arn
  }
}
```

Unlike S3, DynamoDB re-encrypts existing items in place when the key changes, so the cost of the migration is operational rather than a copy job—but the IAM consequence is the same (`kms:Decrypt` and `kms:GenerateDataKey` must be granted to every reader and writer).

### Streams

Disabled. Re-evaluate when a concrete change-driven consumer (webhook, analytics fan-out, search index) is in scope. Enabling later does not require a rebuild.

## Consequences

Positive:

- Predictable, single-digit-KB items. Polling stays single-GetItem and cheap regardless of document length.
- The PK is a deliberate contributor to idempotency, not an incidental choice—pairing it with the extractor's conditional writes gives end-to-end exactly-once semantics on top of at-least-once delivery.
- The extractor's write path is one DDB call, no dual-write coordination with S3.
- Encryption posture is consistent with the storage module's, and the migration story to CMKs is symmetric.
- Streams remain a free option to enable later without a table rebuild.

Negative:

- No second permission gate on the table: a principal with `dynamodb:GetItem` reads results without a separate `kms:Decrypt` check. Acceptable for a portfolio project; revisit before real PII arrives.
- No CloudTrail data-event auditability on individual reads—DynamoDB does not offer object-level read auditing the way S3 server access logging does.
- The OCR'd text and agent trace are not part of the durable answer record. If we ever want them tied to the answer, we either re-derive (text) or look them up by `document_id` in the observability backend (trace).
- A future "extraction history" requirement (multiple runs per document) becomes a schema change rather than a free dimension. Mitigated by the fact that adding a sort key would require a table rebuild regardless of when we do it.

Neutral:

- TTL is enabled but unused at MVP. Costs nothing; gives us a retention knob without a future migration.
- `confidences`, `token_usage`, and `processing_ms` are listed as optional so the extractor can land before `agentic-kie` produces them.

## Alternatives considered

- **Composite key with extraction version (`document_id` + `attempt_id`).** Rejected for MVP—there is no requirement to retain re-runs, and idempotency is handled at the extractor with conditional writes. Re-introduce only when versioned history becomes a real need.
- **Store the full structured result + OCR text + agent trace in DynamoDB.** Rejected—the text and trace are unbounded, push items past the 400 KB cap on long documents, and inflate every poll's RCU cost by 10–50×. They are not part of the answer.
- **Store only an S3 pointer in DynamoDB ("DDB as index").** Rejected—forces every read to do `GetItem` + `GetObject` even for the small, bounded answer, and replaces a clean schema with stringly-typed S3 keys. Useful only if the answer itself were unbounded, which it is not.
- **CMK for the table now.** Deferred—same reasoning as ADR-0004 for the bucket. The right posture for production PII; disproportionate for a portfolio project. The migration is in-place re-encryption, not a copy job, so timing the switch before real data arrives carries no migration cost.
- **AWS-owned key.** Rejected—strictly dominated by the AWS-managed KMS key in DynamoDB, since the latter is free and gives basic CloudTrail visibility.
- **Streams enabled now.** Deferred—no consumer today, and enabling later is non-breaking.

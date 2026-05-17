# Architecture Decision Records

This directory records the significant architectural decisions made in this project. Each ADR captures the context, the options considered, and the reasoning behind the choice — so future contributors understand not just what was decided, but why, and what tradeoffs were accepted.

## Naming conventions

| Element | Rule |
|---|---|
| File name | `NNNN-kebab-case-title.md` |
| Number | 4-digit zero-padded integer, assigned sequentially (`0001`, `0002`, …) |
| Title | Short imperative phrase describing the decision (verb + noun), e.g. `use-event-driven-pipeline` |
| Status | `Proposed` → `Accepted` → `Deprecated` / `Superseded by ADR-NNNN` |

> [!NOTE]
> When a decision is reversed or replaced, mark the old ADR as `Superseded by ADR-NNNN` and link forward. Never delete an ADR.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-event-driven-serverless-pipeline.md) | Event-driven serverless extraction pipeline | Accepted |
| [0002](0002-defer-polling-endpoint.md) | Defer result delivery to a polling endpoint | Accepted |
| [0003](0003-four-layer-bucket-hardening.md) | Four-layer ingestion bucket hardening | Accepted |
| [0004](0004-sse-s3-over-sse-kms.md) | Use SSE-S3 over SSE-KMS for ingestion bucket encryption | Accepted |
| [0005](0005-sqs-dlq-retry-topology.md) | Standard SQS with DLQ-backed retry for the extraction queue | Accepted |
| [0006](0006-document-id-lifecycle.md) | Server-generated document ID, carried by the S3 object key | Accepted |
| [0007](0007-table-schema-and-encryption.md) | Table schema, bounded item size, and encryption posture | Accepted |
| [0008](0008-ecr-registry-stack-and-digest-pinned-images.md) | ECR registry as a separate stack, digest-pinned extractor images | Accepted |
| [0009](0009-extractor-lambda.md) | Extractor Lambda sizing, concurrency, idempotency, observability and IAM posture | Accepted |

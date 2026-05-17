<h1 align="center">Agentic KIE Deployment</h1>
<p align="center">
  <strong>Serverless, event-driven AWS infrastructure for asynchronous key information extraction with LLMs.</strong>
</p>
<p align="center">
<a href="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-staging.yml"><img src="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-staging.yml/badge.svg" alt="Deploy staging"></a>
<a href="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-prod.yml"><img src="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-prod.yml/badge.svg" alt="Deploy prod"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

<p align="center">A client uploads a document to S3 and receives structured fields asynchronously — no blocking on LLM calls, no extraction infrastructure to manage. The pipeline is fully serverless, event-driven, and Terraform-provisioned on AWS.</p>

## Contents

- [Architecture](#architecture)
- [Modules](#modules)
  - [Storage](#storage)
  - [Queue](#queue)
  - [Table](#table)
  - [Extractor](#extractor)
- [Contributing](#contributing)
- [Architecture decisions](docs/adr/README.md)

---

## Architecture

The pipeline is fully asynchronous. A client calls a small presigner Lambda behind an API Gateway HTTP endpoint, which returns a short-lived pre-signed S3 PUT URL. The client uploads the document directly to S3, bypassing API Gateway payload limits entirely. The bucket emits an `Object Created` event to EventBridge, which routes it to an SQS queue with a dead-letter queue and redrive policy for resilience. SQS then triggers the extractor Lambda, packaged as a container image from ECR to accommodate heavier LLM dependencies. The extractor runs the [`agentic-kie`](https://github.com/gafnts/agentic-kie) library against the document and writes the resulting structured record to a DynamoDB table keyed by document ID.

![architecture](./docs/architecture.png)

| Component | Service | Role |
|---|---|---|
| Presigner | Lambda + API Gateway | Issues short-lived pre-signed PUT URLs to clients |
| Ingestion bucket | S3 | Receives uploads directly from clients, emits Object Created events |
| Event router | EventBridge | Routes bucket events to the extraction queue |
| Queue | SQS + DLQ | Buffers events, retries on failure, isolates bad messages |
| Extractor | Lambda (container image) | Runs the agentic LLM extraction loop |
| Store | DynamoDB | Holds structured results, keyed by document ID |

---

## Modules

The infrastructure is organized as small, per-concern Terraform modules wired together at the root in [infra/main.tf](infra/main.tf).

| Module | Path | Status |
|---|---|---|
| `storage` | [infra/modules/storage/](infra/modules/storage/) | Implemented |
| `queue` | [infra/modules/queue/](infra/modules/queue/) | Implemented |
| `table` | [infra/modules/table/](infra/modules/table/) | Implemented |
| `extractor` | [infra/modules/extractor/](infra/modules/extractor/) | Implemented |
| `uploader` | [infra/modules/uploader/](infra/modules/uploader/) | Planned |

### Storage

The ingestion bucket is the entry point of the pipeline. Clients upload documents directly via pre-signed PUT URLs, and the bucket forwards `Object Created` events to EventBridge for downstream routing. The bucket is locked down through four orthogonal hardening layers:

| Layer | Mechanism | What it closes |
|---|---|---|
| Public Access Block | All four block flags enabled | Prevents ACLs or policies from ever making objects public |
| Ownership controls | `BucketOwnerEnforced` | Disables ACLs entirely; every object is owned by the bucket account regardless of uploader |
| TLS-only policy | Deny on `aws:SecureTransport = false` | Enforces HTTPS at the policy layer; old SDKs and misconfigured clients cannot fall back to HTTP |
| Default encryption | SSE-S3 (AES256) | Protects data at rest; AWS manages the key transparently |

EventBridge notifications are enabled on the bucket so object-creation events flow into the rest of the system. The routing rule lives with the queue module.

CORS is configured to allow `PUT` requests from the origins listed in `allowed_upload_origins`, which is the only method clients need to deposit documents.

> [!NOTE]
> The bucket currently uses SSE-S3 (AES256). For workloads ingesting PII or regulated documents, SSE-KMS with a customer-managed key and S3 Bucket Keys enabled provides a second permission gate (`kms:Decrypt` in addition to `s3:GetObject`) and full CloudTrail auditability on every decrypt.

### Queue

The extraction queue sits between the ingestion bucket and the extractor Lambda. An EventBridge rule scoped to the bucket forwards `Object Created` events to a Standard SQS queue, which triggers the extractor. Failed messages are moved to a dead-letter queue after a bounded number of retries so a single poison-pill document cannot burn LLM cost indefinitely.

| Lever | Value | What it controls |
|---|---|---|
| Visibility timeout | `6 × lambda_timeout_seconds` (computed) | Hides an in-flight message long enough to cover the worst-case extractor run plus handoff jitter, eliminating the most common SQS+Lambda misconfiguration |
| `maxReceiveCount` | 3 | Bounds retries on transient failures before the message is shunted to the DLQ |
| Long polling | `receive_wait_time_seconds = 20` | Reduces empty receives and smooths Lambda triggering at no extra cost |
| TLS-only policy | Deny on `aws:SecureTransport = false` (main + DLQ) | Mirrors the bucket's transport posture across the pipeline |
| Source-scoped send | `aws:SourceArn` condition on `events.amazonaws.com` | Closes the confused-deputy class of misconfigurations on the EventBridge → SQS hop |
| Encryption | SSE-SQS (AWS-managed, main + DLQ) | Protects messages at rest without the operational cost of KMS |

The queue does not constrain consumer parallelism; bounding the number of concurrent LLM invocations is the extractor module's job (`maximum_concurrency` on the event source mapping).

> [!NOTE]
> The visibility timeout is derived from `lambda_timeout_seconds` inside the module so the two values cannot drift. The extractor module passes its own timeout through at the root, keeping the queue's hide window in lockstep with the extractor's maximum runtime.

### Table

The results table is the system of record for extractions. The extractor writes one item per document keyed by `document_id` (UUIDv7, minted once at presign), and the polling endpoint reads it back with a single `GetItem`. Holding only the bounded answer (status, structured fields, confidences, model and timing metadata) keeps items in the single-digit-KB range, which keeps polling cheap and stays well clear of DynamoDB's 400 KB item cap. The OCR'd text and the agent trace deliberately live elsewhere (S3 and the observability backend, respectively); see [ADR-0007](docs/adr/0007-table-schema-and-encryption.md) for the full schema contract.

| Lever | Value | What it controls |
|---|---|---|
| Partition key | `document_id` (UUIDv7) | Stable across SQS redeliveries, so retries land on the same row and conditional writes can enforce idempotency |
| Sort key | None | One canonical row per document; extraction history is not a current requirement |
| Billing mode | `PAY_PER_REQUEST` | No capacity planning at portfolio scale; absorbs bursts without throttling |
| Encryption | SSE with AWS-managed KMS key (`aws/dynamodb`) | Free in DynamoDB, adds basic CloudTrail visibility on the encryption context, parity with the storage module's posture |
| Point-in-time recovery | Enabled in both `staging` and `prod` | Cheap insurance against accidental writes or deletes; keeps environments configuration-symmetric |
| TTL | Enabled on `ttl` attribute (unused at MVP) | Retention knob available without a future migration |
| Deletion protection | `prod` only | Prod is protected from accidental destroy; `staging` stays destroyable so `make destroy` works in the iteration loop |
| Streams | Disabled | No change-driven consumer today; enabling later is non-breaking |

Idempotency is split between this module and the extractor: the schema's job is to make retries collide on the same partition key, and the extractor's job is to use conditional writes so a redelivered message cannot clobber a terminal row.

> [!NOTE]
> The table uses the AWS-managed KMS key, not a customer-managed key. For workloads ingesting real PII (names, dates, jurisdictions in extracted fields), switch to a CMK before real data arrives. DynamoDB re-encrypts items in place when the key changes, so the migration is operational rather than a copy job; the IAM consequence (`kms:Decrypt` and `kms:GenerateDataKey` on every reader and writer) mirrors the bucket-side migration sketched in ADR-0004.

### Extractor

The extractor is a container-image Lambda that consumes the extraction queue, runs the [`agentic-kie`](https://github.com/gafnts/agentic-kie) library against each uploaded document, and writes the structured answer to the results table. It is built on a native arm64 runner, deployed digest-pinned (ADR-0008), and bounded explicitly on the consumer side so an ingestion burst cannot run away with parallel LLM cost. See [ADR-0009](docs/adr/0009-extractor-lambda.md) for the full reasoning.

| Lever | Value | What it controls |
|---|---|---|
| Timeout | 120s | 12× the benchmarked single-pass latency (ADR-0001), bounds runaway-invocation cost without truncating provider tail latency |
| Memory / `/tmp` | 2048 MB each | Holds the container image + transitive libraries; vCPU allocation scales with memory |
| Architecture | `arm64` | ~20% cheaper per GB-second on Graviton; native build on `ubuntu-24.04-arm` so no QEMU emulation |
| `batch_size` | 1 | Per-invocation cost is dominated by the LLM call, so batching does not amortize anything and one-message batches keep the failure model simple |
| `maximum_concurrency` | 10 (staging/local), 25 (prod) | Caps parallel LLM fan-out under an ingestion burst, closing the deferral ADR-0005 made |
| Idempotency | Conditional `PutItem` + status-guarded `UpdateItem` | At-least-once SQS delivery cannot clobber a terminal row; redelivered terminal messages are a no-op |
| Cold-start | No provisioned concurrency | Async polling model hides the 3–10s container-image cold start from the user |
| Networking | No VPC | Talks only to AWS APIs and external HTTPS endpoints; no NAT cost, no ENI cold-start penalty |
| Logs | 14d (staging/local), 30d (prod) | Operational telemetry only — LLM telemetry goes to LangSmith |

The Lambda holds no encryption story of its own: environment variables are encrypted with the AWS-managed Lambda key, parity with the rest of the data path. Secrets (LLM provider key, LangSmith key) live in Secrets Manager, one per environment, fetched once at cold start.

> [!NOTE]
> LLM telemetry ships to **LangSmith** (SaaS) at MVP — purpose-built UI, zero infrastructure cost at portfolio scale, OTel-GenAI-compatible migration path to a self-hosted Langfuse or Phoenix when real data lands. See ADR-0009's "Observability" section for the migration boundary.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DevOps strategy (environment and branch models), first-time setup (state backend, IAM roles, ECR registry, GitHub and local AWS configuration), the day-to-day workflow (PRs, prod promotion), and reference material (`make` targets, design notes).

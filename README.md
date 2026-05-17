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
  - [Bucket](#bucket)
  - [Queue](#queue)
  - [Table](#table)
  - [Extractor](#extractor)
  - [Alarms](#alarms)
- [Observability](#observability)
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
| `bucket` | [infra/modules/bucket/](infra/modules/bucket/) | Implemented |
| `queue` | [infra/modules/queue/](infra/modules/queue/) | Implemented |
| `table` | [infra/modules/table/](infra/modules/table/) | Implemented |
| `extractor` | [infra/modules/extractor/](infra/modules/extractor/) | Implemented |
| `alarms` | [infra/modules/alarms/](infra/modules/alarms/) | Implemented |
| `uploader` | [infra/modules/uploader/](infra/modules/uploader/) | Planned |

### Bucket

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
| Encryption | SSE with AWS-managed KMS key (`aws/dynamodb`) | Free in DynamoDB, adds basic CloudTrail visibility on the encryption context, parity with the bucket module's posture |
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

### Alarms

The alarms module owns the alerting plane: one SNS topic per environment, plus an optional email subscription, that every CloudWatch alarm in the stack publishes to. Function-level and queue-level alarms live next to the resources they watch (extractor module, queue module) and reference this topic by ARN, so the alerting fan-out is one resource rather than one-per-alarm.

| Lever | Value | What it controls |
|---|---|---|
| Topic encryption | SSE with AWS-managed KMS key (`alias/aws/sns`) | At-rest encryption for any message body the topic ever holds, parity with the rest of the data path |
| Email subscription | Optional (`alarm_email` tfvar; null disables) | Local/dev runs without notifications; staging/prod set the address per environment. Subscriptions require manual confirmation from the recipient's inbox before delivery starts |
| Topic policy | AWS default (account-only publish) | CloudWatch in the same account can publish without an explicit policy; no cross-account fan-out at this scope |

---

## Observability

The pipeline's observability splits cleanly into two backends, each scoped to what it's good at:

| Concern | Backend | Cadence | What it answers |
|---|---|---|---|
| Operational telemetry | CloudWatch Logs + Alarms → SNS | Minute-to-hour, during incidents | Did the function run? Did it error? Is the DLQ filling up? Is concurrency throttling? |
| LLM telemetry | LangSmith (SaaS) | Week-to-month, during prompt iteration | What did the model see? What did it say? Token usage, schema-validation outcomes, agent tool calls |

The two share a `document_id` correlation key but otherwise have nothing in common; trying to serve both from a single backend compromises both. See [ADR-0009](docs/adr/0009-extractor-lambda.md)'s "Observability" section for the full reasoning, and the [LangSmith NOTE](#extractor) above for the data-residency tradeoff and migration path.

### Alarms

Three CloudWatch alarms cover the operational hot path. All three are 1-of-1 5-minute evaluations, treat missing data as `notBreaching` (idle infrastructure is not a failure), and publish to the alarms module's SNS topic on both alarm and OK transitions.

| Alarm | Source | Fires when | Why it matters |
|---|---|---|---|
| `${function}-errors` | `AWS/Lambda` `Errors` (Sum) | `> 0` over 5 min | Any unhandled exception. With `maxReceiveCount = 3` on the queue, a single bad document fires this up to three times before it lands in the DLQ — the early-warning signal that the DLQ alarm is the confirmation of |
| `${function}-throttles` | `AWS/Lambda` `Throttles` (Sum) | `> 0` over 5 min | Invocations rejected because the function hit its `maximum_concurrency` cap. Throttles mean ingestion is exceeding the planned LLM fan-out budget; either the cap is wrong or there's a burst worth investigating |
| `${dlq}-messages-visible` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` (Max) on the DLQ | `> 0` over 5 min | A message in the DLQ means a document exhausted its three retries. The DLQ is the single source of truth for failed messages (ADR-0005); this alarm is the page on it |

`IteratorAge` is intentionally not wired — it's a Kinesis/DDB Streams metric, not an SQS-Lambda one. `ConcurrentExecutions` is not wired either; it overlaps the Throttles signal at this scale.

### Paging integration (deferred)

The SNS topic accepts an email subscription today; routing to PagerDuty, Opsgenie, or a Slack webhook is deliberately out of scope. At portfolio scale there is no on-call rotation; email subscription is enough to demonstrate the wiring and exercise the alarm → notification path end-to-end. The migration is mechanical when it matters: swap `protocol = "email"` for an `https` subscription pointing at the upstream integration's webhook URL, or add a Lambda subscriber that transforms the SNS payload into the upstream's API shape. The alarms themselves do not change; only the topic's subscriptions do.

> [!NOTE]
> Email subscriptions require the recipient to confirm the SNS subscription from their inbox the first time the topic is created. `terraform apply` does not block on confirmation, and unconfirmed subscriptions stay in `PendingConfirmation` indefinitely without delivering — worth checking after the first deploy in a new environment.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DevOps strategy (environment and branch models), first-time setup (state backend, IAM roles, ECR registry, GitHub and local AWS configuration), the day-to-day workflow (PRs, prod promotion), and reference material (`make` targets, design notes).

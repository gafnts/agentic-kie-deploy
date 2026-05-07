<h1 align="center">Agentic KIE Deployment</h1>
<p align="center">
  <strong>Serverless, event-driven AWS infrastructure for asynchronous key information extraction with LLMs.</strong>
</p>
<p align="center">
<a href="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-dev.yml"><img src="https://github.com/gafnts/agentic-kie-deploy/actions/workflows/deploy-dev.yml/badge.svg" alt="Deploy dev"></a>
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
| `table` | [infra/modules/table/](infra/modules/table/) | Planned |
| `registry` | [infra/modules/registry/](infra/modules/registry/) | Planned |
| `extractor` | [infra/modules/extractor/](infra/modules/extractor/) | Planned |
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
> The visibility timeout is derived from `lambda_timeout_seconds` inside the module so the two values cannot drift. Until the extractor module exists, the input has a placeholder default; the extractor module will pass its own timeout through and that default will be removed.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for prerequisites, project bootstrapping and IAM setup procedure, local AWS profile configuration, available `make` targets, and the full DevOps strategy.

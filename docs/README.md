# Module Reference

The infrastructure is organized as small, per-concern Terraform modules wired together at the root in [infra/main.tf](../infra/main.tf). The order below roughly follows the data flow, with shared stores introduced before their writers (the table before the extractor that fills it). For the system overview and how to use the pipeline, see the [project README](../README.md).

## Contents

- [Uploader](#uploader)
- [Bucket](#bucket)
- [Queue](#queue)
- [Table](#table)
- [Extractor](#extractor)
- [Publisher](#publisher)
- [Analytics](#analytics)
- [Observability](#observability)
  - [Alarms](#alarms)

---

## Uploader

The uploader is the pipeline's front door. An in-account caller signs `POST /uploads` with SigV4 against an API Gateway HTTP API authorized via `AWS_IAM`; the presigner Lambda mints a UUIDv7 `document_id`, composes the `uploads/{yyyy}/{mm}/{dd}/{document_id}` key ([ADR-0006](adr/0006-document-id-lifecycle.md)), and returns the ID alongside a short-lived pre-signed S3 PUT URL. The caller uploads directly to the ingestion bucket, bypassing API Gateway payload limits. See [ADR-0010](adr/0010-uploader-module.md) for the full reasoning.

| Lever | Value | What it controls |
|---|---|---|
| API flavor | API Gateway HTTP API | Cheaper per request and lower latency than REST API; the feature delta (no usage plans, no request validators) does not bite an internal AWS-native caller |
| Authorizer | `AWS_IAM` on `POST /uploads` | Caller signs with the IAM role it already has—no API keys to rotate, no JWT issuer to operate—and the principal ARN lands in CloudTrail by default |
| URL TTL | 600s (tfvar `url_ttl_seconds`, 60–3600 range) | Long enough for retries and slow networks; short enough that a leaked URL is useless within an hour |
| Packaging | Python 3.13, zip | `boto3` + one signing call—the container-image cold-start tax ([ADR-0008](adr/0008-ecr-registry-stack-and-digest-pinned-images.md)/[0009](adr/0009-extractor-lambda.md)) is unjustified here |
| Timeout / memory | 5s / 256MB | One `generate_presigned_url` call; larger memory does not reduce wall-clock |
| Architecture | `arm64` | Same cost reasoning as the extractor; the build is a zip on the right architecture |
| Execution IAM | `s3:PutObject` scoped to `${ingestion_bucket}/uploads/*` | The signed URL inherits the role's grant, so the role must hold the action it grants. Prefix-scoped so a misuse cannot sign URLs for the analytics partition introduced by [ADR-0012](adr/0012-results-module.md) |
| Access logs | API Gateway → CloudWatch (JSON) | `requestId`, source IP, route, status, response length, latency on every request—available without instrumenting the Lambda |
| Concurrency | No reserved or maximum | The presign call is cheap and API Gateway already rate-limits; there is no fan-out to bound (alarms cover throttles at the account ceiling) |

> [!NOTE]
> The route is open to any in-account principal holding `execute-api:Invoke` on the route ARN. The account boundary is the outer perimeter; the extractor's document-type coupling ([ADR-0013](adr/0013-single-tenant-deployment-model.md)) is the inner one. If a future environment cannot rely on the account boundary, the hardening lever is a Lambda authorizer that allowlists principal ARNs—see [ADR-0010](adr/0010-uploader-module.md)'s alternatives section.

---

## Bucket

The ingestion bucket is the entry point of the pipeline. Clients upload documents directly via pre-signed PUT URLs, and the bucket forwards `Object Created` events to EventBridge for downstream routing. The bucket is locked down through four orthogonal hardening layers:

| Layer | Mechanism | What it closes |
|---|---|---|
| Public Access Block | All four block flags enabled | Prevents ACLs or policies from ever making objects public |
| Ownership controls | `BucketOwnerEnforced` | Disables ACLs entirely; every object is owned by the bucket account regardless of uploader |
| TLS-only policy | Deny on `aws:SecureTransport = false` | Enforces HTTPS at the policy layer; old SDKs and misconfigured clients cannot fall back to HTTP |
| Default encryption | SSE-S3 (AES256) | Protects data at rest; AWS manages the key transparently |

EventBridge notifications are enabled on the bucket so object-creation events flow into the rest of the system. The routing rule lives with the queue module.

Three operational settings sit alongside the hardening layers—they aren't part of the access-control posture, but the bucket needs them to be operationally sound rather than just locked down:

| Setting | Mechanism | What it gives us |
|---|---|---|
| Versioning | `Enabled` on the ingestion bucket | Recovery from accidental overwrite or delete; non-current versions are expired by the lifecycle rule below |
| Server-access logging | Sibling `${bucket}-logs` bucket (PAB enabled, AES256, 90-day expiry on logs) | Request-level audit trail of every operation on the ingestion bucket, independent of CloudTrail data events |
| Lifecycle / tiering | `STANDARD_IA` at 30d, `GLACIER_IR` at 90d, expire at 365d; noncurrent versions expire at 30d; incomplete multipart uploads abort at 7d | Bounds steady-state storage cost without manual cleanup; cold-tier transitions match the access pattern (documents are read once at extraction, rarely after) |

> [!NOTE]
> The bucket currently uses SSE-S3 (AES256). For workloads ingesting PII or regulated documents, SSE-KMS with a customer-managed key and S3 Bucket Keys enabled provides a second permission gate (`kms:Decrypt` in addition to `s3:GetObject`) and full CloudTrail auditability on every decrypt.

---

## Queue

The extraction queue sits between the ingestion bucket and the extractor Lambda. An EventBridge rule scoped to the bucket and the `uploads/` key prefix forwards `Object Created` events to a Standard SQS queue, which triggers the extractor. Failed messages are moved to a dead-letter queue after a bounded number of retries so a single poison-pill document cannot burn LLM cost indefinitely.

| Lever | Value | What it controls |
|---|---|---|
| Visibility timeout | `6 × lambda_timeout_seconds` (computed) | Hides an in-flight message long enough to cover the worst-case extractor run plus handoff jitter, eliminating the most common SQS+Lambda misconfiguration |
| `maxReceiveCount` | 3 (single-pass), 2 (agentic) | Bounds retries on transient failures before the message is shunted to the DLQ. Follows `extractor_flavor`: agentic failures are mostly logic (a non-terminating loop), not transient, so retrying an expensive doomed run buys nothing ([ADR-0016](adr/0016-agentic-flavor-deployment.md)) |
| Long polling | `receive_wait_time_seconds = 20` | Reduces empty receives and smooths Lambda triggering at no extra cost |
| TLS-only policy | Deny on `aws:SecureTransport = false` (main + DLQ) | Mirrors the bucket's transport posture across the pipeline |
| Source-scoped send | `aws:SourceArn` condition on `events.amazonaws.com` | Closes the confused-deputy class of misconfigurations on the EventBridge → SQS hop |
| Key-prefix filter | EventBridge pattern matches `object.key` prefix `uploads/` | Defense-in-depth: any future sibling prefix in the ingestion bucket (e.g. cached OCR text written next to a source document) cannot fan out into LLM invocations without an explicit rule change |
| Encryption | SSE-SQS (AWS-managed, main + DLQ) | Protects messages at rest without the operational cost of KMS |

The queue does not constrain consumer parallelism; bounding the number of concurrent LLM invocations is the extractor module's job (`maximum_concurrency` on the event source mapping).

> [!NOTE]
> The visibility timeout is derived from `lambda_timeout_seconds` inside the module so the two values cannot drift. The extractor module passes its own timeout through at the root, keeping the queue's hide window in lockstep with the extractor's maximum runtime.

---

## Table

The results table is the system of record for extractions. The extractor writes one item per document keyed by `document_id` (UUIDv7, minted once at presign) and reads it back with a single `GetItem` to reconcile redeliveries. Holding only the bounded answer (status, structured fields, model and timing metadata) keeps items in the single-digit-KB range, which keeps reads cheap and stays well clear of DynamoDB's 400 KB item cap. The OCR'd text and the agent trace deliberately live elsewhere (S3 and the observability backend, respectively); see [ADR-0007](adr/0007-table-schema-and-encryption.md) for the full schema contract. Results reach the caller through the analytics partition, not a polling endpoint ([ADR-0011](adr/0011-s3-as-result-delivery.md)).

| Lever | Value | What it controls |
|---|---|---|
| Partition key | `document_id` (UUIDv7) | Stable across SQS redeliveries, so retries land on the same row and conditional writes can enforce idempotency |
| Sort key | None | One canonical row per document; extraction history is not a current requirement |
| Billing mode | `PAY_PER_REQUEST` | No capacity planning at portfolio scale; absorbs bursts without throttling |
| Encryption | SSE with AWS-managed KMS key (`aws/dynamodb`) | Free in DynamoDB, adds basic CloudTrail visibility on the encryption context, parity with the bucket module's posture |
| Point-in-time recovery | Enabled in both `staging` and `prod` | Cheap insurance against accidental writes or deletes; keeps environments configuration-symmetric |
| TTL | Enabled on `ttl` attribute (unused at MVP) | Retention knob available without a future migration |
| Deletion protection | `prod` only | Prod is protected from accidental destroy; `staging` stays destroyable so `make destroy` works in the iteration loop |
| Streams | Enabled (`NEW_IMAGE`) | Drives the publisher's fan-out to the analytics partition ([ADR-0014](adr/0014-split-results-module.md)) |

Idempotency is split between this module and the extractor: the schema's job is to make retries collide on the same partition key, and the extractor's job is to use conditional writes so a redelivered message cannot clobber a terminal row.

> [!NOTE]
> The table uses the AWS-managed KMS key, not a customer-managed key. For workloads ingesting real PII (names, dates, jurisdictions in extracted fields), switch to a CMK before real data arrives. DynamoDB re-encrypts items in place when the key changes, so the migration is operational rather than a copy job; the IAM consequence (`kms:Decrypt` and `kms:GenerateDataKey` on every reader and writer) mirrors the bucket-side migration sketched in [ADR-0004](adr/0004-sse-s3-over-sse-kms.md).

---

## Extractor

The extractor is a container-image Lambda that consumes the extraction queue, runs the [`agentic-kie`](https://github.com/gafnts/agentic-kie) library against each uploaded document, and writes the structured answer to the results table. It is built on a native arm64 runner, deployed digest-pinned ([ADR-0008](adr/0008-ecr-registry-stack-and-digest-pinned-images.md)), and bounded explicitly on the consumer side so an ingestion burst cannot run away with parallel LLM cost. See [ADR-0009](adr/0009-extractor-lambda.md) for the full reasoning.

| Lever | Value | What it controls |
|---|---|---|
| Timeout | 120s | 12× the benchmarked single-pass latency ([ADR-0001](adr/0001-event-driven-serverless-pipeline.md)), bounds runaway-invocation cost without truncating provider tail latency |
| Memory / `/tmp` | 2048 MB each | Holds the container image + transitive libraries; vCPU allocation scales with memory |
| Architecture | `arm64` | ~20% cheaper per GB-second on Graviton; native build on `ubuntu-24.04-arm` so no QEMU emulation |
| `batch_size` | 1 | Per-invocation cost is dominated by the LLM call, so batching does not amortize anything and one-message batches keep the failure model simple |
| `maximum_concurrency` | 10 (staging/local), 25 (prod) | Caps parallel LLM fan-out under an ingestion burst, closing the deferral [ADR-0005](adr/0005-sqs-dlq-retry-topology.md) made |
| `extractor_flavor` | `single_pass` (default), `agentic` | Which [`agentic-kie`](https://github.com/gafnts/agentic-kie) strategy the handler builds—one structured call vs. a ReAct loop over the document. Selectable per environment at deploy time; it drives the whole parameter profile (`max_iterations`, `maxReceiveCount`) so flipping a flavor is a one-variable change ([ADR-0016](adr/0016-agentic-flavor-deployment.md)) |
| `max_iterations` (agentic only) | ~30 | Caps LangGraph supersteps (`recursion_limit`, ≈ 2× the LLM-call count), bounding a non-terminating agent run. `n/a` for single-pass, which has no loop |
| Idempotency | Conditional `PutItem` + status-guarded `UpdateItem` | At-least-once SQS delivery cannot clobber a terminal row; redelivered terminal messages are a no-op |
| Cold-start | No provisioned concurrency | The asynchronous delivery model hides the 3-10s container-image cold start from the caller |
| Networking | No VPC | Talks only to AWS APIs and external HTTPS endpoints; no NAT cost, no ENI cold-start penalty |
| Logs | 14d (staging/local), 30d (prod) | Operational telemetry only—LLM telemetry goes to LangSmith |

The Lambda holds no encryption story of its own: environment variables are encrypted with the AWS-managed Lambda key, parity with the rest of the data path. Secrets (LLM provider key, LangSmith key) live in Secrets Manager, one per environment, fetched once at cold start.

> [!NOTE]
> LLM telemetry ships to **LangSmith** (SaaS) at MVP—purpose-built UI, zero infrastructure cost at portfolio scale, OTel-GenAI-compatible migration path to a self-hosted Langfuse or Phoenix when real data lands. See [ADR-0009](adr/0009-extractor-lambda.md)'s "Observability" section for the migration boundary.

---

## Publisher

The publisher is the feed that carries terminal extractions out of DynamoDB and into the analytics partition. A zip-packaged Lambda subscribes to the results table's DynamoDB Streams, filters to terminal rows, and writes each result payload as JSON to the analytics bucket at `extractions/{yyyy}/{mm}/{dd}/{document_id}.json`—the same address the caller learned at presign time. It carries its own dead-letter queue and alarms. It was split out of the former `results` module so the disposable feed no longer shares a plan blast radius with the durable store ([ADR-0014](adr/0014-split-results-module.md)); the resource-level reasoning lives in [ADR-0012](adr/0012-results-module.md), and S3-as-delivery in [ADR-0011](adr/0011-s3-as-result-delivery.md).

| Lever | Value | What it controls |
|---|---|---|
| Packaging | Python 3.13, zip | `boto3` plus one `PutObject` per record—no heavy dependencies, so the container-image cold-start tax ([ADR-0008](adr/0008-ecr-registry-stack-and-digest-pinned-images.md)) the extractor pays is unjustified here, same as the presigner |
| Timeout / memory | 30s / 256MB | One S3 `PutObject` per record across a batch of up to 100; the work is I/O-bound, so more memory does not reduce wall-clock |
| Architecture | `arm64` | Same Graviton cost reasoning as the rest of the pipeline; the build is a zip on the right architecture |
| Stream subscription | DynamoDB Streams event source mapping, `starting_position = LATEST` | Fans the table's terminal writes out to S3; the table owns the stream, the publisher owns the consumer |
| Filter criteria | `eventName` in {`INSERT`, `MODIFY`}, `status` in {`succeeded`, `failed`} | Only terminal rows publish; in-progress and non-terminal updates never reach the Lambda, so no partial result ever lands in the analytics partition |
| `batch_size` / batching window | 100 records / 5s | Stream records are tiny and the consumer does one PUT each, so batching amortizes invocation overhead; the 5s window bounds result-delivery tail latency |
| `maximum_retry_attempts` | 3 | Mirrors the extraction queue's `maxReceiveCount = 3` for retry-budget symmetry across the pipeline |
| Partial-batch failure | `bisect_batch_on_function_error` + `ReportBatchItemFailures` | One poison record does not re-drive the whole batch; the failure is isolated and only the failed record retries before landing in the DLQ |
| Execution IAM | Stream read scoped to the table's stream ARN; `s3:PutObject` scoped to `${analytics_bucket}/${results_prefix}/*`; `sqs:SendMessage` on its own DLQ | Least-privilege on both ends of the feed; the write scope cannot drift from the analytics read path because `results_prefix` is single-sourced at the root ([ADR-0014](adr/0014-split-results-module.md)) |
| Concurrency | No reserved or maximum | The PUT is cheap and stream shards already bound parallelism; throttles are covered by an alarm at the account ceiling |
| Logs | 14d (staging/local), 30d (prod) | Operational telemetry only, parity with the rest of the pipeline |

Exhausted batches land in a dedicated dead-letter queue (14-day retention, SSE-SQS, TLS-only policy), and a `${publisher-dlq}-messages-visible` alarm pages on it. Function-level `errors` and `throttles` alarms round out the publisher's coverage in the [Observability](#observability) section.

> [!NOTE]
> The result payload the publisher writes and the Glue table's column list in the analytics module are a documented lockstep that crosses both the Python/HCL boundary and (since the split) a module boundary. The guard is unchanged: the integration smoke test asserts the round-trip object lands and matches, and the Glue SerDe's `ignore.malformed.json` tolerates additive drift. See [ADR-0014](adr/0014-split-results-module.md) and [ADR-0012](adr/0012-results-module.md)'s post-implementation notes.

---

## Analytics

The analytics module is the durable store and its query surface—the half of the former `results` module that holds the irreplaceable record ([ADR-0014](adr/0014-split-results-module.md)). The `extractions` bucket holds the result objects the publisher writes; the caller subscribes to its `s3:ObjectCreated:*` events to learn the moment a result lands ([ADR-0011](adr/0011-s3-as-result-delivery.md)); and the same objects back a projected Glue `extractions` table queried through a dedicated Athena workgroup.

| Lever | Value | What it controls |
|---|---|---|
| Bucket hardening | PAB (all four flags), `BucketOwnerEnforced`, TLS-only deny policy, SSE-S3 (AES256) | The analytics store is locked down with the same four-layer posture as the ingestion bucket |
| Storage tier | `STANDARD` only; no cold-tier transition; no current-version expiration | Athena queries the objects on demand and cannot transparently restore from Glacier, and the results are the durable record of work, so they never tier out or expire |
| Versioning + lifecycle | Versioning enabled; noncurrent versions expire at 30d; incomplete multipart uploads abort at 7d | Recovery from accidental overwrite without unbounded version-storage cost |
| Server-access logging | Sibling `${bucket}-logs` bucket (PAB, AES256, 90-day expiry) | Request-level audit trail on every operation, independent of CloudTrail |
| Event notifications | EventBridge enabled on the bucket | The caller's `s3:ObjectCreated:*` subscription on the `extractions/` prefix fires the moment a result object lands |
| Catalog | Glue `extractions` table, partition projection on `year`/`month`/`day` | Athena computes partitions from the path template at query time, so no crawler and no `MSCK REPAIR` job is needed |
| SerDe | OpenX JSON SerDe, `ignore.malformed.json = true`, nested maps typed as `string` | Returns raw JSON for the evolving `extracted_fields`/`confidences` (queryable with `json_extract`) without coupling the table to their schema; `token_usage` stays a `struct` so per-window cost is a direct `SUM(token_usage.input)` |
| Athena workgroup | Dedicated workgroup; `bytes_scanned_cutoff_per_query` (1 GiB default); enforced config; pinned result location (SSE-S3) | A cost boundary (Athena bills per TB scanned) and a query-routing isolation point; query-results objects are debugging artifacts and expire at 7d |

The Glue database (`{project}_{environment}_analytics`) and the workgroup (`{project}-{environment}-analytics`) are named for the subsystem, not the dataset; the table keeps the dataset name, `extractions`, so a query reads naturally as `analytics.extractions`. After the split, nothing in the query layer carries the word `results`, which now denotes exactly one thing: the DynamoDB [table](#table).

> [!NOTE]
> The analytics bucket is the result-delivery surface, but each consumer's `s3:GetObject` grant on `extractions/*` deliberately lives on the consumer's side, not in this module: the module exposes `bucket_arn` for each consumer to scope its own grant against. An instance serves one schema but any number of in-account consumers ([ADR-0013](adr/0013-single-tenant-deployment-model.md) / [ADR-0017](adr/0017-refine-tenancy-unit-to-schema.md)). The bucket uses SSE-S3 (AES256); for regulated result data, the same SSE-KMS migration sketched for the ingestion bucket and the table applies.

---

## Observability

The pipeline's observability splits cleanly into two backends, each scoped to what it's good at:

| Concern | Backend | Cadence | What it answers |
|---|---|---|---|
| Operational telemetry | CloudWatch Logs + Alarms → SNS | Minute-to-hour, during incidents | Did the function run? Did it error? Is the DLQ filling up? Is concurrency throttling? |
| LLM telemetry | LangSmith (SaaS) | Week-to-month, during prompt iteration | What did the model see? What did it say? Token usage, schema-validation outcomes, agent tool calls |

The two share a `document_id` correlation key but otherwise have nothing in common; trying to serve both from a single backend compromises both. See [ADR-0009](adr/0009-extractor-lambda.md)'s "Observability" section for the full reasoning.

### Alarms

The alarms module owns the alerting plane: one SNS topic per environment, plus an optional email subscription, that every CloudWatch alarm in the stack publishes to. Function-level and queue-level alarms live next to the resources they watch (uploader, extractor, queue, and publisher modules) and reference this topic by ARN, so the alerting fan-out is one resource rather than one-per-alarm.

| Lever | Value | What it controls |
|---|---|---|
| Topic encryption | SSE with AWS-managed KMS key (`alias/aws/sns`) | At-rest encryption for any message body the topic ever holds, parity with the rest of the data path |
| Email subscription | Optional (`alarm_email` tfvar; null disables) | Local/dev runs without notifications; staging/prod set the address per environment. Subscriptions require manual confirmation from the recipient's inbox before delivery starts |
| Topic policy | AWS default (account-only publish) | CloudWatch in the same account can publish without an explicit policy; no cross-account fan-out at this scope |

Eight CloudWatch alarms cover the operational hot path. Each is a 1-of-1 5-minute evaluation, treats missing data as `notBreaching` (idle infrastructure is not a failure), and publishes to the topic above on both alarm and OK transitions.

| Alarm | Source | Fires when | Why it matters |
|---|---|---|---|
| `${extractor}-errors` | `AWS/Lambda` `Errors` (Sum) on the extractor | `> 0` over 5 min | Any unhandled exception. A single bad document fires this once per delivery attempt (up to the queue's `maxReceiveCount`—3 for single-pass, 2 for agentic) before it lands in the DLQ—the early-warning signal that the DLQ alarm is the confirmation of |
| `${extractor}-throttles` | `AWS/Lambda` `Throttles` (Sum) on the extractor | `> 0` over 5 min | Invocations rejected because the function hit its `maximum_concurrency` cap. Throttles mean ingestion is exceeding the planned LLM fan-out budget; either the cap is wrong or there's a burst worth investigating |
| `${presigner}-errors` | `AWS/Lambda` `Errors` (Sum) on the presigner | `> 0` over 5 min | The presigner does one `generate_presigned_url` call—non-zero errors imply an IAM regression or a malformed request that slipped past API Gateway |
| `${presigner}-throttles` | `AWS/Lambda` `Throttles` (Sum) on the presigner | `> 0` over 5 min | The presigner has no reserved or maximum concurrency ([ADR-0010](adr/0010-uploader-module.md)); throttles imply the account concurrency ceiling is being approached |
| `${dlq}-messages-visible` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` (Max) on the DLQ | `> 0` over 5 min | A message in the DLQ means a document exhausted its `maxReceiveCount` retries (3 single-pass, 2 agentic). The DLQ is the single source of truth for failed messages ([ADR-0005](adr/0005-sqs-dlq-retry-topology.md)); this alarm is the page on it |
| `${publisher}-errors` | `AWS/Lambda` `Errors` (Sum) on the publisher | `> 0` over 5 min | An unhandled exception in the Streams consumer. Result objects silently stop reaching S3 while the extractor keeps writing terminal rows to DynamoDB |
| `${publisher}-throttles` | `AWS/Lambda` `Throttles` (Sum) on the publisher | `> 0` over 5 min | The publisher has no reserved or maximum concurrency; throttles stall result publishing and leave `succeeded`/`failed` rows without matching S3 objects |
| `${publisher-dlq}-messages-visible` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` (Max) on the publisher DLQ | `> 0` over 5 min | A stream batch exhausted `maximum_retry_attempts`. The single source of truth for failed batches, mirroring the extractor DLQ alarm ([ADR-0014](adr/0014-split-results-module.md)) |

`IteratorAge` is not wired: on the extractor it does not apply (an SQS-Lambda consumer, not a streams one), and on the publisher the DLQ and Errors alarms already catch a stalled or failing consumer. `ConcurrentExecutions` is not wired either; it overlaps the Throttles signal at this scale.

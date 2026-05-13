# ADR-0009: Extractor Lambda — Sizing, Concurrency, Idempotency, and IAM Posture

## Status

Proposed (2026-05-11)

## Context

ADR-0001 fixed the extractor as a container-image Lambda that runs `agentic-kie` against each uploaded document and writes the structured answer to DynamoDB. ADR-0005 fixed the queue topology and explicitly deferred consumer-side parallelism to this ADR. ADR-0006 fixed the `document_id` contract carried in the S3 object key. ADR-0007 fixed the table schema and explicitly delegated the "operation half" of idempotency (conditional writes, status state machine) to the extractor. ADR-0008 fixed the registry shape and the `extractor_image_digest` tfvar contract.

What is left for this ADR is the Lambda itself and the moving parts it owns end-to-end:

1. **Sizing**: timeout, memory, ephemeral storage, architecture. These set the cost floor of every invocation and feed back into the queue's visibility timeout via the `lambda_timeout_seconds` input the queue module already exposes.
2. **Consumer parallelism**: `batch_size`, `maximum_concurrency`, and partial-batch responses on the SQS event source mapping. ADR-0005 noted that the queue intentionally does not constrain LLM fan-out; without a cap here, an ingestion burst scales Lambda up to the account's concurrent-execution limit (default 1,000) and drives a corresponding spike of parallel LLM calls. `maximum_concurrency` is the lever that bounds that fan-out.
3. **Idempotency operation half**: how the extractor turns "stable PK across redeliveries" (ADR-0006/0007) into "redelivered messages cannot clobber a terminal row." Status state machine: `pending → succeeded | failed`.
4. **IAM execution role**: the minimum grants needed for the extractor to receive from SQS, read the source object from S3, write to the results table, fetch the provider API key, and emit logs — with the `Environment` tag-deny guard (ADR-0008-style) holding the per-env blast radius.
5. **Provider secrets**: where the LLM API key lives and how the Lambda obtains it. The choice is between Secrets Manager (rotation story, per-secret IAM) and SSM Parameter Store (cheaper, no rotation).
6. **Cold start posture**: container-image Lambdas have multi-second cold starts. Provisioned concurrency is the lever; whether to use it at portfolio scale is a tradeoff, not a default.
7. **VPC, networking, and egress**: the extractor only talks to AWS APIs and the LLM provider's public HTTPS endpoint. No VPC unless there is a private dependency, which there is not.
8. **Observability**: structured JSON logs keyed by `document_id`. ADR-0007 explicitly placed the agent trace here (CloudWatch Logs Insights now; Langfuse/Phoenix later) — this ADR settles the log shape and retention.

Two upstream contracts narrow the design space:

- The event payload is `Object Created` from EventBridge, forwarded verbatim by SQS. `document_id` is parsed out of `s3.object.key` (ADR-0006); a parse failure is a poison-pill and must not retry.
- The table schema (ADR-0007) is the answer and only the answer; OCR text and agent traces are *not* persisted to DynamoDB. The extractor's hot path is one DDB call, not a dual-write.

## Decision

### Stack layout

A new module at `infra/modules/extractor/`, wired from `infra/main.tf` next to `storage`, `queue`, and `table`. The module owns the Lambda function, its execution role and inline policies, the SQS event source mapping, the CloudWatch log group, and the function-level alarms. The registry stack stays separate (ADR-0008); this module consumes the repository via a `data "aws_ecr_repository"` lookup and the image via the `extractor_image_digest` tfvar.

```
infra/modules/extractor/
  main.tf
  variables.tf
  outputs.tf
  terraform.tf
```

`infra/variables.tf` gains:

```hcl
variable "extractor_image_digest" {
  description = "Immutable digest (sha256:...) of the extractor container image to deploy"
  type        = string
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.extractor_image_digest))
    error_message = "extractor_image_digest must be a sha256 digest, e.g. sha256:abc...123."
  }
}

variable "llm_provider_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the LLM provider API key"
  type        = string
}
```

The deploy workflow already resolves the digest (`steps.push.outputs.digest`); the `apply` job gains `-var=extractor_image_digest=…` and the TODO in `.github/workflows/deploy-dev.yml:130` is closed.

### Sizing

| Lever              | Value                | Reasoning                                                                                                                                                                                              |
| ------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Timeout            | **120 s**            | ADR-0001's benchmark put Gemini Standard single-pass at ~10 s end-to-end; the 12× headroom absorbs provider tail latency without giving a runaway invocation an unbounded cost ceiling.                |
| Memory             | **2048 MB**          | The container image carries `agentic-kie` plus transitive libraries; warm invocations are dominated by network wait, not CPU, but the larger memory footprint also raises the vCPU allocation and shortens cold-start init. |
| Ephemeral storage  | **2048 MB** (`/tmp`) | Long agreements in the Kleister NDA corpus are several MB on disk; doubling the default leaves headroom for OCR intermediate files without paying for the 10 GB ceiling.                               |
| Architecture       | **arm64**            | Graviton is ~20% cheaper per GB-second than x86_64 with no behavioral difference for an I/O-bound workload (the LLM call dominates; the CPU barely participates). Built on a native arm64 runner — see note below. |
| Runtime            | Container image      | Settled in ADR-0001 and ADR-0008.                                                                                                                                                                       |

> [!NOTE]
> The `build-and-push` job moves to GitHub's native arm64 runner (`runs-on: ubuntu-24.04-arm`), free on public repositories. This avoids QEMU emulation entirely: builds run at native speed (roughly 2–3× faster than emulated arm64 on an x86_64 host), and the build environment matches Lambda's runtime architecture, eliminating a class of "works in CI, fails at deploy" bugs from emulated syscall differences. The job keeps `docker/setup-buildx-action@v3` (the named builder is required for `buildx build --push`) and does **not** need `docker/setup-qemu-action`. The build line becomes `docker buildx build --platform=linux/arm64 --push -t "$REPO_URL:$IMAGE_TAG" src/extractor/` — note that `--push` moves into the build invocation, since a buildx platform-targeted build cannot stay in the local Docker image store.
>
> Only the image build moves to arm64. The `plan` and `apply` jobs stay on `ubuntu-latest` (x86_64), so `.terraform.lock.hcl` is unaffected — it already covers `linux_amd64` via `make lock`. If a future change ever moves Terraform jobs to arm64 Linux, extend the `lock` target in the Makefile with `-platform=linux_arm64` and regenerate before the first run on the new runner.

The module passes its timeout into the queue module as `lambda_timeout_seconds = 120`, which makes the visibility timeout `720 s` (`6 × 120`). This closes the placeholder default in `infra/modules/queue/variables.tf:17`.

### Event source mapping

```hcl
resource "aws_lambda_event_source_mapping" "extraction" {
  event_source_arn                   = var.queue_arn
  function_name                      = aws_lambda_function.extractor.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.max_concurrency # default 10
  }
}
```

- **`batch_size = 1`**: every Lambda invocation processes exactly one document. The extractor's hot path is an LLM call whose duration is dominated by the model, not by setup; batching does not amortize anything meaningful, but it does turn a single bad document into a `ReportBatchItemFailures` ceremony. One-message batches keep the failure model simple: a function-level error is the message's failure.
- **`function_response_types = ["ReportBatchItemFailures"]`**: even at `batch_size = 1` we declare it explicitly. This makes the response shape forward-compatible if the batch size ever changes, and makes the "do not redeliver" path (parse failures → log and ack) explicit rather than relying on swallowed exceptions.
- **`maximum_concurrency = 10`** (default, override per env): this is the cost ceiling ADR-0005 explicitly handed off here. Ten concurrent LLM calls is a deliberate portfolio-scale knob — high enough to keep a small burst flowing, low enough that a 10,000-document accident does not bill ten thousand parallel `Gemini Standard` calls. `prod` raises this only when there is a real reason to.

### Idempotency and the status state machine

The schema's half of idempotency is the stable `document_id` PK (ADR-0007). The operation half is two DDB calls per invocation, both conditional:

1. **First write (claim)**:
   ```python
   table.put_item(
       Item={"document_id": doc_id, "status": "pending", "created_at": now_iso},
       ConditionExpression="attribute_not_exists(document_id)",
   )
   ```
   - Success: this invocation owns the document.
   - `ConditionalCheckFailedException`: another delivery already claimed it. Re-read the row; if `status in {succeeded, failed}` it is already terminal — log, return success, **ack** the message. If `status == pending`, the prior invocation is still in-flight or crashed; the visibility timeout (720 s) is the synchronization window, so this delivery returns failure and is retried after the window expires.

2. **Terminal write (complete or fail)**:
   ```python
   table.update_item(
       Key={"document_id": doc_id},
       UpdateExpression="SET #s = :new, completed_at = :now, ...",
       ConditionExpression="#s = :pending",
       ExpressionAttributeNames={"#s": "status"},
       ExpressionAttributeValues={":new": "succeeded", ":pending": "pending", ":now": now_iso},
   )
   ```
   The condition guards against a stale extractor finishing after a redelivered one already wrote a terminal status. A failed conditional update here is logged but not retried — the row already has a terminal answer.

A parse failure (`document_id` not extractable from `s3.object.key`, malformed event envelope) is a poison-pill: log the offending payload at `ERROR`, **do not** raise, return success so the message is acked and goes nowhere. The DLQ is reserved for transient retries that exceeded `maxReceiveCount`, not for malformed inputs.

### IAM execution role

The role is created inside the module, named `agentic-kie-deploy-${env}-extractor-exec`, and tagged `Environment = ${env}` so the `iam/` stack's `DenyTouchingOtherEnvs` guard is honored. Inline policies, not managed:

| Statement              | Action                                                                 | Resource                                       |
| ---------------------- | ---------------------------------------------------------------------- | ---------------------------------------------- |
| `SqsConsume`           | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`    | `var.queue_arn`                                |
| `IngestionReadObject`  | `s3:GetObject`                                                         | `${var.ingestion_bucket_arn}/*`                |
| `ResultsWrite`         | `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:GetItem`          | `var.results_table_arn`                        |
| `ProviderSecretRead`   | `secretsmanager:GetSecretValue`                                        | `var.llm_provider_secret_arn`                  |
| `LogsWrite`            | `logs:CreateLogStream`, `logs:PutLogEvents`                            | `${aws_cloudwatch_log_group.extractor.arn}:*`  |

ECR pull is *not* in the execution role — ADR-0008's repository policy already grants `lambda.amazonaws.com` pull rights scoped by `aws:SourceArn` to this function's ARN. CloudWatch Logs `CreateLogGroup` is not granted either; the module owns the log group as a resource so the function never needs to create it.

### Provider secrets

The LLM API key is stored in AWS Secrets Manager, one secret per environment, name `agentic-kie-deploy/${env}/llm-provider`. The secret is **created out-of-band** (manual `aws secretsmanager create-secret` per env, documented in `CONTRIBUTING.md`) and its ARN passed in via `var.llm_provider_secret_arn`. Terraform manages the IAM grant; Terraform does not manage the secret value, because committing a secret reference that disappears with `terraform destroy` is a footgun and rotation will not flow through `terraform apply` anyway.

The Lambda fetches the secret once at cold start (top-level module scope), caches it in memory, and reuses it for warm invocations. The 15-minute Lambda execution-environment lifetime bounds staleness; rotation strategy beyond that is deferred to a future ADR alongside CMK encryption (the cluster of "before real data arrives" decisions).

The secret ARN is passed to the Lambda as `LLM_PROVIDER_SECRET_ARN` env var, not the value itself, so secret material never lands in Lambda configuration or CloudTrail.

### Networking

No VPC. The extractor talks only to AWS APIs (reachable via the AWS service endpoints from outside a VPC) and the LLM provider's public HTTPS endpoint. Placing the function in a VPC would force a NAT Gateway (~$32/mo per AZ) and ENI cold-start cost for zero security benefit at this scope. When a private dependency arrives — a VPC-only RDS, an internal service — we revisit.

### Observability

- **Log group**: `aws_cloudwatch_log_group.extractor` at `/aws/lambda/${function_name}`, retention **14 days** in `local` and `dev`, **30 days** in `prod`. Cheap, bounded, no eternal log accumulation.
- **Log shape**: structured JSON, one event per line, with `document_id`, `attempt` (SQS `ApproximateReceiveCount`), `event_source_message_id`, and `model_version` on every line. This makes CloudWatch Logs Insights queries by `document_id` viable as the ADR-0007 trace store, until the trace data volume justifies Langfuse/Phoenix.
- **Function-level error destination**: *not* set. SQS retries via redrive and routes to the DLQ; a separate Lambda-level on-failure destination would double-count failures.
- **Alarms**: deferred to the same future observability ADR that ADR-0005 deferred queue alarms to.

### Cold starts and provisioned concurrency

Container-image Lambdas with multi-GB dependencies have 3–10 s cold starts. For an asynchronous SQS-driven workload where the client polls, a one-time cold-start tax is invisible to the user. Provisioned concurrency at portfolio scale costs more than it saves. **No provisioned concurrency** at MVP; revisit if `IteratorAge` on the queue or end-to-end latency complaints make it a real problem.

`SnapStart` is unavailable for container-image Lambdas, so the cold-start lever is provisioned concurrency or nothing.

### Module wiring

`infra/main.tf` gains:

```hcl
data "aws_ecr_repository" "extractor" {
  name = "${var.project_name}-${var.environment}-extractor"
}

module "extractor" {
  source                  = "./modules/extractor"
  function_name           = "${var.project_name}-${var.environment}-extractor"
  image_uri               = "${data.aws_ecr_repository.extractor.repository_url}@${var.extractor_image_digest}"
  timeout_seconds         = 120
  memory_mb               = 2048
  ephemeral_storage_mb    = 2048
  architecture            = "arm64"
  max_concurrency         = var.environment == "prod" ? 25 : 10
  queue_arn               = module.queue.queue_arn
  ingestion_bucket_arn    = module.storage.bucket_arn
  results_table_arn       = module.table.table_arn
  llm_provider_secret_arn = var.llm_provider_secret_arn
  log_retention_days      = var.environment == "prod" ? 30 : 14
  environment             = var.environment
}
```

And the queue wiring is updated to pass through the function's timeout, removing the placeholder default flagged in `infra/modules/queue/variables.tf:14-17`:

```hcl
module "queue" {
  source                 = "./modules/queue"
  name                   = "${var.project_name}-${var.environment}-extraction"
  source_bucket_name     = module.storage.bucket_name
  lambda_timeout_seconds = module.extractor.timeout_seconds
}
```

### Module responsibilities

| Module / Stack       | Responsibility                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `infra/registry/`    | Repository, lifecycle policy, repository policy (ADR-0008). Output: `repository_url`.                                       |
| CI `build-and-push`  | Build image (arm64), push to env repo, publish digest as job output. Existing job; needs `--platform=linux/arm64`.          |
| `infra/modules/extractor/` | Lambda function, execution role + inline policies, event source mapping, log group. Inputs: image URI, queue/bucket/table ARNs, secret ARN. |
| `infra/modules/queue/` | Receives `lambda_timeout_seconds` from the extractor module; placeholder default removed.                                  |
| `iam/`               | Unchanged; `PowerUserAccess` already covers Lambda + IAM create needed by the deploy roles.                                 |

## Consequences

Positive:

- The cost ceiling on an ingestion burst is explicit (`maximum_concurrency`) and lives next to the function it bounds, closing the deferral ADR-0005 made.
- Idempotency is end-to-end: stable PK (ADR-0006/0007) + conditional first write + status-guarded terminal update means at-least-once SQS delivery cannot clobber a terminal row, and a redelivered already-terminal message is a no-op.
- The visibility timeout can no longer drift from the function timeout; the queue module's placeholder default disappears and the relationship is enforced by Terraform wiring.
- Parse failures are a no-retry path by construction: malformed inputs do not waste three `maxReceiveCount` attempts before reaching the DLQ.
- IAM grants are the minimum needed for the data plane, scoped to specific ARNs, and the `Environment`-tag deny guard from `iam/` still holds.
- arm64 is a ~20% cost reduction with no behavioral change; the only cost is a one-line `buildx` change in CI.
- No VPC means no NAT cost and no ENI cold-start penalty.

Negative:

- Container-image cold starts (3–10 s) are accepted as-is. The async polling model hides them from the user, but a synchronous read endpoint would have to revisit this.
- `maximum_concurrency = 10` is a guess; the right value depends on real ingestion patterns. It is a single tfvar to bump.
- The provider secret is created out-of-band; this is one more first-time-setup step per env, documented but easy to forget. Mitigated by failing loudly: the Lambda's first invocation will throw a clear "secret not found" on cold start.
- `batch_size = 1` gives up the small wins of batching (a single Lambda processing two trivial documents in one invocation). The simpler failure model is worth the cost.
- Function-level partial-batch responses (`ReportBatchItemFailures`) are over-engineered for `batch_size = 1`, but locking the shape in now avoids a behavioral change if the batch size ever moves.

Neutral:

- No provisioned concurrency. Revisit only if `IteratorAge` or end-to-end latency complaints make a cold-start tax a real problem.
- No function-level alarms. Same deferral as ADR-0005 made for the queue; both move together in a future observability ADR.
- Log retention bound at 14/30 days. CloudWatch Logs Insights remains the trace store of record until volume justifies Langfuse/Phoenix (ADR-0007).
- AES256 / AWS-managed keys throughout the data path (S3, SQS, DynamoDB). The Lambda has no encryption story of its own; environment variables are encrypted with the AWS-managed Lambda key, which is parity with the rest of the data stores.

## Alternatives considered

- **Zip Lambda with layers instead of a container image.** Rejected — ADR-0001 and ADR-0008 settled the packaging question on dependency-size grounds. Re-litigating it here would invalidate the registry stack.
- **`batch_size > 1` (e.g. 5 or 10).** Rejected at MVP — the per-message processing time is dominated by the LLM call, so batching does not amortize anything meaningful, and it complicates the failure model (partial batch failure handling becomes load-bearing rather than forward-compatibility scaffolding).
- **No `maximum_concurrency` cap.** Rejected — explicitly the failure mode ADR-0005 warned about: an ingestion burst translating one-to-one into parallel LLM bills. The cap is the cost-bound on accidents.
- **x86_64 architecture.** Rejected for cost. The CI change is mechanical and arm64 has no behavioral implications for Python + HTTP clients.
- **Provisioned concurrency at MVP.** Rejected — cold starts are invisible to a polling client; provisioned concurrency is a steady-state cost for a benefit nobody currently feels.
- **Place the function in a VPC.** Rejected — no private dependency exists; a VPC would force a NAT Gateway and ENI cold-start cost for zero security benefit.
- **SSM Parameter Store for the provider API key.** Viable, slightly cheaper, no rotation story. Secrets Manager wins for the explicit "secret" semantics and the rotation path it leaves open. Re-evaluate if cost becomes material.
- **Function-level Dead Letter Config (SNS or SQS destination on `OnFailure`) in addition to the queue's DLQ.** Rejected — doubles the failure surface (two places to look) for no extra information. The SQS DLQ is the single source of truth for failed messages.
- **Lambda Powertools / event-source-mapping filtering on the queue.** Rejected — adds dependency surface and configuration complexity for no current need. Filtering at the EventBridge rule (already scoped to the bucket) is enough.
- **CMK encryption on the Lambda environment variables.** Deferred — same reasoning as ADR-0004 / 0007 / 0008. Migrate the entire data path at the same boundary (before real PII arrives).
- **Webhook callback on completion** (mentioned in ADR-0002 as a deferred result-delivery option). Out of scope; this ADR is the consumer-side of the existing pipeline, not the result-delivery channel.

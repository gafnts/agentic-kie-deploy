# ADR-0009: Extractor Lambda (Sizing, Concurrency, Idempotency, Observability and IAM Posture)

## Status

Accepted (2026-05-11)

## Context

ADR-0001 fixed the extractor as a container-image Lambda that runs `agentic-kie` against each uploaded document and writes the structured answer to DynamoDB. ADR-0005 fixed the queue topology and explicitly deferred consumer-side parallelism to this ADR. ADR-0006 fixed the `document_id` contract carried in the S3 object key. ADR-0007 fixed the table schema and explicitly delegated the "operation half" of idempotency (conditional writes, status state machine) to the extractor. ADR-0008 fixed the registry shape and the `extractor_image_digest` tfvar contract.

What is left for this ADR is the Lambda itself and the moving parts it owns end-to-end:

1. **Sizing**: timeout, memory, ephemeral storage, architecture. These set the cost floor of every invocation and feed back into the queue's visibility timeout via the `lambda_timeout_seconds` input the queue module already exposes.
2. **Consumer parallelism**: `batch_size`, `maximum_concurrency`, and partial-batch responses on the SQS event source mapping. ADR-0005 noted that the queue intentionally does not constrain LLM fan-out; without a cap here, an ingestion burst scales Lambda up to the account's concurrent-execution limit (default 1,000) and drives a corresponding spike of parallel LLM calls. `maximum_concurrency` is the lever that bounds that fan-out.
3. **Idempotency operation half**: how the extractor turns "stable PK across redeliveries" (ADR-0006/0007) into "redelivered messages cannot clobber a terminal row." Status state machine: `pending → succeeded | failed`.
4. **IAM execution role**: the minimum grants needed for the extractor to receive from SQS, read the source object from S3, write to the results table, fetch the secrets it depends on, and emit logs — with the `Environment` tag-deny guard (ADR-0008-style) holding the per-env blast radius.
5. **Secrets**: where the LLM API key and the LangSmith API key live and how the Lambda obtains them. Two secrets, same shape: Secrets Manager (explicit "secret" semantics, rotation path), one per environment, created out-of-band.
6. **Cold start posture**: container-image Lambdas have multi-second cold starts. Provisioned concurrency is the lever; whether to use it at portfolio scale is a tradeoff, not a default.
7. **VPC, networking, and egress**: the extractor only talks to AWS APIs and external HTTPS endpoints (LLM provider, LangSmith). No VPC unless there is a private dependency, which there is not.
8. **Observability**: operational telemetry (CloudWatch) and LLM telemetry (LangSmith) are two distinct concerns with different cadences and different debuggers. ADR-0007 placed the agent trace in the extractor's territory; this ADR settles which backend serves it.

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

variable "langsmith_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the LangSmith API key"
  type        = string
}
```

Both deploy workflows already resolve the digest as a `build-and-push` job output (`steps.push.outputs.digest`). Wiring it through differs between environments because the two workflows have different apply patterns:

- **`deploy-dev.yml`**: the `apply` job runs `terraform apply` directly. The digest is injected there as `-var=extractor_image_digest=${{ needs.build-and-push.outputs.image_digest }}`, closing the TODO at `.github/workflows/deploy-dev.yml:130-131`.
- **`deploy-prod.yml`**: the `apply` job consumes a saved plan artifact (`tfplan.prod`) produced by an earlier `plan` job. The digest must therefore be baked into the *plan*, not the apply — passed as `-var=extractor_image_digest=…` to `make ci-plan` in the `plan` job, closing the TODO at `.github/workflows/deploy-prod.yml:137-138`. The apply step consumes the plan bytes verbatim and inherits the digest from there.

The asymmetry is load-bearing: a saved-plan workflow that takes a `-var` at apply time would diverge from the plan that was reviewed, which is exactly the property the saved-plan pattern exists to prevent.

### Sizing

| Lever              | Value                | Reasoning                                                                                                                                                                                              |
| ------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Timeout            | **120 s**            | ADR-0001's benchmark put Gemini Standard single-pass at ~10 s end-to-end; the 12× headroom absorbs provider tail latency without giving a runaway invocation an unbounded cost ceiling.                |
| Memory             | **2048 MB**          | The container image carries `agentic-kie` plus transitive libraries; warm invocations are dominated by network wait, not CPU, but the larger memory footprint also raises the vCPU allocation and shortens cold-start init. |
| Ephemeral storage  | **2048 MB** (`/tmp`) | Long agreements in the Kleister NDA corpus are several MB on disk; doubling the default leaves headroom for OCR intermediate files without paying for the 10 GB ceiling.                               |
| Architecture       | **arm64**            | Graviton is ~20% cheaper per GB-second than x86_64 with no behavioral difference for an I/O-bound workload (the LLM call dominates; the CPU barely participates). Built on a native arm64 runner — see note below. |
| Runtime            | Container image      | Settled in ADR-0001 and ADR-0008.                                                                                                                                                                       |

> [!NOTE]
> The `build-and-push` jobs in **both** `.github/workflows/deploy-dev.yml` and `.github/workflows/deploy-prod.yml` move to GitHub's native arm64 runner (`runs-on: ubuntu-24.04-arm`), free on public repositories. This avoids QEMU emulation entirely: builds run at native speed (roughly 2–3× faster than emulated arm64 on an x86_64 host), and the build environment matches Lambda's runtime architecture, eliminating a class of "works in CI, fails at deploy" bugs from emulated syscall differences. The job keeps `docker/setup-buildx-action@v3` (the named builder is required for `buildx build --push`) and does **not** need `docker/setup-qemu-action`. The build line becomes `docker buildx build --platform=linux/arm64 --push -t "$REPO_URL:$IMAGE_TAG" src/extractor/` — note that `--push` moves into the build invocation, since a buildx platform-targeted build cannot stay in the local Docker image store.
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

Put together, the two conditional writes give the system a property it can't get from SQS alone: exactly-once semantics on top of at-least-once delivery. The pipeline can redeliver a message ten times, three Lambdas can race on the same document, and the DynamoDB row will end up with exactly one answer — the first one written by whichever Lambda crossed the finish line first.

The cost is bounded: a redelivery for an already-terminal document is a couple of cheap DynamoDB calls (PutItem that fails the condition, GetItem to read the status), not another LLM call. The LLM call — the expensive thing — only ever runs for the Lambda that won the claim.

### IAM execution role

The role is created inside the module, named `agentic-kie-deploy-${env}-extractor-exec`, and tagged `Environment = ${env}` so the `iam/` stack's `DenyTouchingOtherEnvs` guard is honored. Inline policies, not managed:

| Statement              | Action                                                                 | Resource                                                              |
| ---------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `SqsConsume`           | `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`    | `var.queue_arn`                                                       |
| `IngestionReadObject`  | `s3:GetObject`                                                         | `${var.ingestion_bucket_arn}/*`                                       |
| `ResultsWrite`         | `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:GetItem`          | `var.results_table_arn`                                               |
| `SecretsRead`          | `secretsmanager:GetSecretValue`                                        | `var.llm_provider_secret_arn`, `var.langsmith_secret_arn`             |
| `LogsWrite`            | `logs:CreateLogStream`, `logs:PutLogEvents`                            | `${aws_cloudwatch_log_group.extractor.arn}:*`                         |

ECR pull is *not* in the execution role — ADR-0008's repository policy already grants `lambda.amazonaws.com` pull rights scoped by `aws:SourceArn` to this function's ARN. CloudWatch Logs `CreateLogGroup` is not granted either; the module owns the log group as a resource so the function never needs to create it. The two secrets share one `SecretsRead` statement rather than splitting into two near-identical statements; the resource list is the audit boundary either way.

### Secrets

The Lambda depends on two long-lived secrets: the LLM provider API key (used on the hot path, every invocation) and the LangSmith API key (used to ship traces, also every invocation). Both follow the same shape — Secrets Manager, one secret per environment, created out-of-band, ARN passed in via tfvar — so they share an operational story.

The LLM provider key lives at `agentic-kie-deploy/${env}/llm-provider`, the LangSmith key at `agentic-kie-deploy/${env}/langsmith`. Both are created with `aws secretsmanager create-secret` per environment, documented as a one-time step in `CONTRIBUTING.md`, and their ARNs are passed through `var.llm_provider_secret_arn` and `var.langsmith_secret_arn` respectively. Terraform manages the IAM grants but not the secret values: committing a secret reference that disappears with `terraform destroy` is a footgun, and rotation will not flow through `terraform apply` anyway. Out-of-band provisioning makes the lifecycles independent on purpose.

The Lambda fetches both secrets once at cold start (top-level module scope), caches them in memory, and reuses them across warm invocations. The 15-minute Lambda execution-environment lifetime bounds staleness; rotation strategy beyond that is deferred to a future ADR alongside CMK encryption (the cluster of "before real data arrives" decisions).

Four environment variables wire the Lambda into both secrets and the LangSmith project, each with a deliberate provenance:

| Env var                    | Value                                                                    | Set by                                                              |
| -------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| `LLM_PROVIDER_SECRET_ARN`  | ARN of the LLM provider secret                                           | Terraform, from `var.llm_provider_secret_arn`                       |
| `LANGSMITH_SECRET_ARN`     | ARN of the LangSmith secret                                              | Terraform, from `var.langsmith_secret_arn`                          |
| `LANGSMITH_PROJECT`        | `${var.project_name}-${var.environment}` (e.g. `agentic-kie-deploy-dev`) | Terraform, composed at the root from `project_name` + `environment` |
| `LANGSMITH_API_KEY`        | The fetched LangSmith API key value                                      | Lambda at cold start, after `secretsmanager:GetSecretValue`         |

Only ARNs and the (env-derived) project name are passed to Lambda configuration; no secret material lands in CloudTrail or in `terraform.tfstate`. The LLM provider key is passed to the library's client as a constructor argument rather than via env var, so it is not visible in `printenv`-style debugging output. `LANGSMITH_PROJECT` is intentionally composed at the root from `var.project_name` and `var.environment` rather than exposed as a tfvar — the value is fully determined by the environment, and a misalignment between the env's resource names and the LangSmith project name is the kind of bug that produces "where did my traces go" mystery sessions.

### Networking

No VPC. The extractor talks only to AWS APIs (reachable via the AWS service endpoints from outside a VPC) and external HTTPS endpoints (LLM provider, LangSmith). Placing the function in a VPC would force a NAT Gateway (~$32/mo per AZ) and ENI cold-start cost for zero security benefit at this scope. When a private dependency arrives — a VPC-only RDS, an internal service — we revisit.

### Observability

Observability splits into two concerns that live at very different cadences. **Operational telemetry** is what Lambda already produces — was the function triggered, did it succeed, how long did it take, which SQS attempt is this — and it answers questions in the minute-to-hour range during an incident. **LLM telemetry** is what the model saw and said — the exact prompt, the exact completion, token usage, schema validation outcomes, tool calls for agentic runs — and it answers questions in the week-to-month range during prompt iteration, drift investigation, and post-hoc "why did doc X get this wrong?" debugging. They share the same `document_id` correlation key but otherwise have nothing in common, and trying to serve both from a single backend produces compromises that hurt both. Two backends, scoped tightly to what each is good at, is the cleaner answer.

#### Operational telemetry

This is the cheap, bounded half. Lambda writes structured JSON to a module-owned log group at `/aws/lambda/${function_name}`, one event per line, with `document_id`, `attempt` (SQS `ApproximateReceiveCount`), `event_source_message_id`, and `handler_outcome` (`succeeded` | `failed` | `redelivery_noop` for the Outcome B path in the idempotency section) on every line. Retention is **14 days** in `local` and `dev`, **30 days** in `prod` — long enough to investigate a recent incident, short enough that the log bill never becomes a line item anyone has to defend.

No function-level error destination is configured. SQS already routes failed messages to the DLQ via the redrive policy (ADR-0005), and attaching a separate Lambda-level on-failure destination would double-count the same failure in two places, which makes runbook investigation harder, not easier. The DLQ is the single source of truth for failed messages.

Function-level alarms (`Errors`, `Throttles`, `IteratorAge` on the event source mapping) are deferred to the same future observability ADR that ADR-0005 deferred queue alarms to. The two sets of alarms are tightly coupled — Lambda errors push queue depth up, queue depth pushes `IteratorAge` up — and designing them in one pass is cleaner than splitting them across two ADRs.

#### LLM telemetry

ADR-0007 placed the agent trace in the extractor module's territory explicitly, with the upgrade path described as "CloudWatch Logs Insights now; Langfuse or Phoenix later." That framing assumed a hand-rolled JSON schema in CloudWatch as the MVP backend, with a self-hosted trace store as the next step. At portfolio scale, both ends of that progression are heavier than the project needs. Designing a CloudWatch JSON schema means owning the schema, the Logs Insights queries that interrogate it, and the migration plan when it stops scaling — substantial work to recreate, badly, what dedicated LLM-observability tools provide out of the box. Self-hosting Langfuse or Phoenix is the production-grade answer but is a stack of its own (Fargate, RDS, S3, IAM, an ADR) for an extraction pipeline that, at this scale, runs a handful of documents per day.

The path of least resistance for a portfolio project is **LangSmith**, used standalone — no LangChain dependency. The `langsmith` Python SDK exposes a `@traceable` decorator that wraps any function and ships its inputs, outputs, latency, and metadata to LangSmith's hosted backend. The library's `Extractor` strategies wrap one (single-pass) or several (agentic) LLM calls; instrumenting the call sites with `@traceable` produces a trace per document, with one root span per extraction and child spans for each tool invocation in agentic mode. Token usage, model + version, prompt, completion, and latency are captured automatically when the SDK detects the provider's SDK in the call stack; for providers it doesn't recognize natively, the same metadata can be passed explicitly via the decorator's `metadata` parameter. There is no schema for us to design and no Logs Insights query for us to write — the UI gives all of that for free.

LangSmith is provisioned out-of-band, the same shape as the LLM provider secret: one project per environment (`agentic-kie-deploy-dev`, `agentic-kie-deploy-prod`), one API key per project, stored in Secrets Manager at `agentic-kie-deploy/${env}/langsmith`, ARN passed in via the `langsmith_secret_arn` tfvar declared above. The execution role's `SecretsRead` statement covers both keys via a single resource list. The Lambda picks the project up from the `LANGSMITH_PROJECT` env var (derived inside the module from `${var.project_name}-${var.environment}`) and the API key from `LANGSMITH_API_KEY`, which is populated at cold start after `secretsmanager:GetSecretValue` on `LANGSMITH_SECRET_ARN`. The LLM provider key is passed to the library's client as a constructor argument.

One Lambda-specific implementation detail is worth recording explicitly because it is the kind of thing that disappears into "the traces are flaky" if it is not surfaced now. The `langsmith` SDK batches trace uploads asynchronously to avoid blocking the request path. On a long-running web server this is invisibly correct; on Lambda, the execution environment is frozen the moment the handler returns, and any batch that has not been flushed by then is lost. The handler must therefore call `langsmith.client.flush()` (or use the SDK's context manager) in a `finally` block, after the DynamoDB terminal write and before returning. The alternative — running the SDK in synchronous mode — adds 50–200 ms to every invocation for the round-trip to LangSmith's API, which is real cost at scale; explicit flushing pays the same network cost but only once at the end, in parallel with the handler's wind-down. The flush call is cheap; forgetting it is silent.

> [!NOTE]
> LangSmith is a SaaS. Prompts and completions leave AWS and live at LangChain's API for the duration of LangSmith's retention. At portfolio scale this is fine — the same calculus that justified SSE-S3 over SSE-KMS in ADR-0004, the AWS-managed key for DynamoDB in ADR-0007, and AES256 for the registry in ADR-0008 applies here: no real PII, no regulatory perimeter, and the tooling cost of treating the prompt as classified data is disproportionate to the threat model. The note that matters is *when* that posture should change: the same boundary at which the CMK migration happens, before real documents begin arriving. At that point the LLM telemetry backend should move to a self-hosted store (Langfuse and Arize Phoenix are the obvious candidates; both are open-source, both accept OTel GenAI data, both have first-class trace inspection UIs), and the prompt-redaction layer that ADR-0004's deferred CMK migration also implies belongs in the same change. LangSmith's data model is OTel-GenAI-compatible at the API level, so the migration is an export-and-replay plus a config change in the SDK, not a re-instrumentation pass through the library.

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
  langsmith_secret_arn    = var.langsmith_secret_arn
  langsmith_project       = "${var.project_name}-${var.environment}"
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
| CI `build-and-push`  | Build image (arm64) on a native arm64 runner, push to env repo, publish digest as job output. Applied to **both** `deploy-dev.yml` and `deploy-prod.yml`: switches `runs-on` to `ubuntu-24.04-arm` and uses `docker buildx build --platform=linux/arm64 --push`. |
| `infra/modules/extractor/` | Lambda function, execution role + inline policies, event source mapping, log group. Inputs: image URI, queue/bucket/table ARNs, both secret ARNs. |
| `infra/modules/queue/` | Receives `lambda_timeout_seconds` from the extractor module; placeholder default removed.                                  |
| `iam/`               | Unchanged; `PowerUserAccess` already covers Lambda + IAM create needed by the deploy roles.                                 |

## Consequences

Positive:

- The cost ceiling on an ingestion burst is explicit (`maximum_concurrency`) and lives next to the function it bounds, closing the deferral ADR-0005 made.
- Idempotency is end-to-end: stable PK (ADR-0006/0007) + conditional first write + status-guarded terminal update means at-least-once SQS delivery cannot clobber a terminal row, and a redelivered already-terminal message is a no-op.
- The visibility timeout can no longer drift from the function timeout; the queue module's placeholder default disappears and the relationship is enforced by Terraform wiring.
- Parse failures are a no-retry path by construction: malformed inputs do not waste three `maxReceiveCount` attempts before reaching the DLQ.
- IAM grants are the minimum needed for the data plane, scoped to specific ARNs, and the `Environment`-tag deny guard from `iam/` still holds.
- arm64 is a ~20% cost reduction with no behavioral change for an I/O-bound workload; the cost is one `runs-on` swap and one `buildx` invocation in CI, on free native runners.
- LLM telemetry is in a tool purpose-built for it (LangSmith), at zero infrastructure cost at portfolio scale; the migration path to a self-hosted backend is OTel-compatible.
- No VPC means no NAT cost and no ENI cold-start penalty.

Negative:

- Container-image cold starts (3–10 s) are accepted as-is. The async polling model hides them from the user, but a synchronous read endpoint would have to revisit this.
- `maximum_concurrency = 10` is a guess; the right value depends on real ingestion patterns. It is a single tfvar to bump.
- The two secrets are created out-of-band; this is two more first-time-setup steps per env, documented but easy to forget. Mitigated by failing loudly: the Lambda's first invocation throws a clear "secret not found" on cold start.
- `batch_size = 1` gives up the small wins of batching (a single Lambda processing two trivial documents in one invocation). The simpler failure model is worth the cost.
- Function-level partial-batch responses (`ReportBatchItemFailures`) are over-engineered for `batch_size = 1`, but locking the shape in now avoids a behavioral change if the batch size ever moves.
- Prompts and completions live at a third-party SaaS (LangSmith) for the duration of its retention. Acceptable for portfolio scale; the migration target and timing are recorded in the Observability NOTE.

Neutral:

- No provisioned concurrency. Revisit only if `IteratorAge` or end-to-end latency complaints make a cold-start tax a real problem.
- No function-level alarms. Same deferral as ADR-0005 made for the queue; both move together in a future observability ADR.
- LangSmith is the trace store of record at MVP; CloudWatch holds operational telemetry only. The migration path to self-hosted Langfuse or Phoenix is in the same cluster of "before real data arrives" decisions as the CMK switch (ADR-0004 / 0007 / 0008).
- AES256 / AWS-managed keys throughout the data path (S3, SQS, DynamoDB). The Lambda has no encryption story of its own; environment variables are encrypted with the AWS-managed Lambda key, which is parity with the rest of the data stores.

## Alternatives considered

- **Zip Lambda with layers instead of a container image.** Rejected — ADR-0001 and ADR-0008 settled the packaging question on dependency-size grounds. Re-litigating it here would invalidate the registry stack.
- **`batch_size > 1` (e.g. 5 or 10).** Rejected at MVP — the per-message processing time is dominated by the LLM call, so batching does not amortize anything meaningful, and it complicates the failure model (partial batch failure handling becomes load-bearing rather than forward-compatibility scaffolding).
- **No `maximum_concurrency` cap.** Rejected — without a cap, an ingestion burst scales Lambda up to the account's concurrent-execution limit (default 1,000) and drives a corresponding spike of parallel LLM calls. The cap is the cost-bound on accidents.
- **x86_64 architecture.** Rejected for cost. The CI change is mechanical (one `runs-on` swap, one `buildx` invocation) and arm64 has no behavioral implications for Python + HTTP clients.
- **QEMU emulation on x86_64 runners instead of native arm64 runners.** Rejected — 2–3× slower builds for no benefit on a public repo where native arm64 runners are free. Emulation is also a source of subtle "works in CI, fails at deploy" syscall-difference bugs.
- **Provisioned concurrency at MVP.** Rejected — cold starts are invisible to a polling client; provisioned concurrency is a steady-state cost for a benefit nobody currently feels.
- **Place the function in a VPC.** Rejected — no private dependency exists; a VPC would force a NAT Gateway and ENI cold-start cost for zero security benefit.
- **SSM Parameter Store for the API keys.** Viable, slightly cheaper, no rotation story. Secrets Manager wins for the explicit "secret" semantics and the rotation path it leaves open. Re-evaluate if cost becomes material.
- **Function-level Dead Letter Config (SNS or SQS destination on `OnFailure`) in addition to the queue's DLQ.** Rejected — doubles the failure surface (two places to look) for no extra information. The SQS DLQ is the single source of truth for failed messages.
- **Expose `LANGSMITH_PROJECT` as a tfvar.** Rejected — the value is fully determined by `var.project_name` and `var.environment`. Making it an input invites drift between the LangSmith project and the rest of the env's resource names, which is the kind of bug that produces silent "where did my traces go" sessions.
- **CMK encryption on the Lambda environment variables.** Deferred — same reasoning as ADR-0004 / 0007 / 0008. Migrate the entire data path at the same boundary (before real PII arrives).
- **Webhook callback on completion** (mentioned in ADR-0002 as a deferred result-delivery option). Out of scope; this ADR is the consumer-side of the existing pipeline, not the result-delivery channel.
- **CloudWatch Logs with a hand-rolled LLM telemetry schema.** Rejected — moves the schema design, the Logs Insights queries, and the eventual migration plan into our codebase, for no benefit at portfolio scale. Reproduces what LangSmith provides out of the box, less well.
- **Self-hosted Langfuse at MVP.** Deferred — the right answer once real data lands or trace volume exceeds LangSmith's free tier, but it is a Fargate-plus-RDS stack with its own ADR. The migration is in scope for the same boundary as the CMK switch (ADR-0004 / 0007 / 0008).
- **Arize Phoenix at MVP.** Same as Langfuse — strong tool, deferred for the same reason. Equally viable as the Phase-2 target; the choice between Langfuse and Phoenix is one for the future ADR.
- **OpenTelemetry GenAI conventions instrumented at MVP, shipped to CloudWatch.** Considered — would maximize backend portability up-front, but requires running an OTel collector and building the CloudWatch sink ourselves. LangSmith's SDK accomplishes the same instrumentation with one decorator and offers OTel export as a migration path, so the portability is preserved without the up-front collector work.
- **Synchronous LangSmith mode** (rather than batched-with-explicit-flush). Rejected — adds 50–200 ms of network round-trip to every invocation, when an explicit flush in a `finally` block pays the same cost once at the end of the handler.

# ADR-0005: Standard SQS with DLQ-Backed Retry for the Extraction Queue

## Status

Proposed (2026-05-07)

## Context

ADR-0001 fixed the eventing topology: S3 emits `Object Created` to EventBridge, which routes to an SQS queue that triggers the extractor Lambda, with a dead-letter queue and redrive policy. That decision settled *what* sits between S3 and the extractor; this ADR settles *how* that queue is configured. It does not revisit the topology.

Three operational parameters drive the queue's behavior under failure:

- **Queue type** — Standard vs FIFO. FIFO buys per-group ordering and exactly-once processing at the cost of a 300 msg/s baseline (3,000 with batching) and per-message-group overhead.
- **Visibility timeout** — the window during which a message is hidden from other consumers after delivery. Must exceed the worst-case extractor execution time, or in-flight work will be redelivered while still running, multiplying LLM cost and risking duplicate writes.
- **`maxReceiveCount` on the redrive policy** — how many delivery attempts before a message is shunted to the DLQ. Too low and transient LLM/provider failures land in the DLQ permanently; too high and a poison-pill document burns retries (and dollars) before isolation.

The extractor is a container Lambda running `agentic-kie`. The benchmark referenced in ADR-0001 puts the winning configuration (Gemini Standard, single-pass) at ~10s end-to-end, but real-world tail latency from the LLM provider is unbounded in principle. Documents are independent: there is no per-document ordering requirement, and idempotency is enforced downstream by the extractor's DynamoDB write being keyed on `doc_id` (to be defined in a later ADR alongside the table module).

Beyond those three parameters, the queue also needs a resource policy, transport-layer hardening, and a polling configuration. None of these are load-bearing decisions on their own, but each is a known footgun in S3 → EventBridge → SQS pipelines and is recorded here so they are not mistaken for accidents of the implementation.

## Decision

Provision a **Standard SQS** queue with the following shape:

| Setting | Value | Reasoning |
|---|---|---|
| Queue type | Standard | No ordering requirement; documents are independent and keyed by `doc_id` downstream |
| Visibility timeout | `6 × lambda_timeout_seconds` (computed) | Covers worst-case execution plus EventBridge/SQS handoff jitter without redelivering in-flight work[^1] |
| `maxReceiveCount` | 3 | Two retries past the first attempt; absorbs transient LLM/provider failures without burning cost on poison pills |
| Message retention (main) | 4 days (default) | Bounds how long an undelivered message lingers; aligns with operator response window |
| Message retention (DLQ) | 14 days (max) | Maximizes the window for inspection and redrive of failed messages |
| Long polling | `receive_wait_time_seconds = 20` | Smooths Lambda triggering and reduces empty receives at no additional cost |
| Encryption | SSE-SQS (AWS-managed) | Mirrors the SSE-S3 reasoning in ADR-0004; no real PII at the portfolio stage |
| DLQ | Separate `*-dlq` queue, same encryption | Isolates poison pills for inspection; redrive is a manual operator action |

The queue module takes `lambda_timeout_seconds` as an input variable and computes the visibility timeout from it in a local. The two values cannot drift, eliminating the most common SQS+Lambda misconfiguration.

Two further hardening decisions, applied to both queues:

- **Resource policy with `aws:SourceArn` condition.** The queue policy grants `sqs:SendMessage` to `events.amazonaws.com`, scoped to the EventBridge rule's ARN. Without this scoping the rule attaches successfully but messages silently fail to land — a footgun worth recording as an explicit decision rather than a code-only convention.
- **`DenyInsecureTransport`.** Both the main queue and the DLQ deny any `sqs:*` action over non-TLS connections, mirroring the bucket policy from ADR-0003 so the transport-layer posture is uniform across the pipeline.

[^1]: AWS recommends setting visibility timeout to at least six times the consumer's processing time (see *Amazon SQS visibility timeout* documentation).

## Consequences

Positive:
- Poison-pill documents are isolated after 3 attempts rather than retried indefinitely; LLM cost is bounded per document
- Visibility timeout is derived from the extractor timeout, eliminating duplicate processing caused by `visibility_timeout < execution_time`
- DLQ retention at the maximum gives a 14-day window to diagnose and redrive without losing the message
- Standard queue's effectively-unlimited throughput leaves headroom for any realistic ingestion burst
- The `aws:SourceArn` condition closes the confused-deputy class of misconfiguration on the EventBridge → SQS hop

Negative:
- Standard SQS allows occasional duplicate delivery; the extractor must remain idempotent at the `doc_id` level (to be enforced by the DynamoDB write in a later ADR)
- `maxReceiveCount = 3` is a guess for the transient-vs-permanent failure boundary; if real traffic shows transient LLM failures clustering above 3, the value will need tuning
- Manual DLQ redrive means a human is in the loop for any persistent failure — acceptable at portfolio scale, would need automation under real traffic

Neutral:
- Switching to SSE-KMS later mirrors the ADR-0004 migration and would happen at the same boundary (real data arriving)
- Queue depth and DLQ-occupancy CloudWatch alarms (`ApproximateAgeOfOldestMessage` on the main queue, `ApproximateNumberOfMessagesVisible > 0` on the DLQ) are deferred to a later observability ADR; their omission is intentional, not an oversight

## Alternatives considered

- **FIFO queue**: rejected — buys ordering and exactly-once semantics that this workload does not need, at the cost of a hard throughput ceiling and per-message-group overhead. Documents are independent.
- **No DLQ, infinite retry**: rejected — a single malformed document would loop forever, accumulating LLM cost on every retry. The DLQ is the cost-bound on failure.
- **`maxReceiveCount = 1` (no retry)**: rejected — every transient provider hiccup becomes a DLQ entry requiring manual redrive, inverting the operator/cost tradeoff.
- **Direct `visibility_timeout_seconds` input** (not derived from the Lambda timeout): rejected — exposes the same number in two modules and invites drift the first time the Lambda timeout is changed without updating the queue.

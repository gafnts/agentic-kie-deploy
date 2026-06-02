# ADR-0015: Load-Testing Strategy

## Status

Proposed (2026-06-01).

## Context

The pipeline is feature-complete: every module in the README's table is implemented, both smoke paths pass, and the operational plane (eight CloudWatch alarms, a DLQ on each consumer, the SQS redrive topology) is wired. What has never been exercised is the system *under arrival pressure*. Every test to date drives exactly one document through the pipeline; we have a benchmark for single-document quality and latency ([ADR-0001](0001-event-driven-serverless-pipeline.md): ~91.5% F1, ~$0.007/doc, <10s) but no evidence for how the pipeline behaves when documents arrive in bulk or steadily over time.

The pipeline is deliberately *not* built to scale infinitely. The extractor's `maximum_concurrency` (10 staging, 25 prod) is a cost guardrail that caps parallel LLM fan-out ([ADR-0009](0009-extractor-lambda.md)), and the SQS queue is the shock absorber that buffers a burst the extractor cannot immediately drain ([ADR-0005](0005-sqs-dlq-retry-topology.md)). So the question a load test answers here is not "what is the peak RPS"—it is **"does the system degrade gracefully (buffer and drain) rather than fail (error and DLQ) under realistic arrival patterns, and do the alarms tell the truth while it happens?"**

This ADR settles what we measure, against what envelope, at what fidelity, and with what pass/fail bar—before any traffic is generated or any dollar is spent.

### What the system can absorb

Steady-state throughput is `maximum_concurrency ÷ per-document latency`. At the benchmarked ~10s single-pass latency:

| Env | `maximum_concurrency` | Steady-state capacity |
|---|---|---|
| staging | 10 | ~1 doc/s (~60/min) |
| prod | 25 | ~2.5 doc/s (~150/min) |

These are the numbers every prediction below is derived from.

### The provider budget is not the binding constraint

The extractor calls Gemini once per document, so the pipeline's request rate equals its throughput: ~60 RPM at staging concurrency, ~150 RPM at prod. The deployment runs on a **Gemini Tier 1** key:

| Limit | Tier 1 | Staging burst draw | Headroom |
|---|---|---|---|
| RPM | 4,000 | ~60 | ~65× |
| TPM (input) | 4,000,000 | ~1.8M worst case | ~2× |
| RPD | ~150,000 | ~400/day across both runs | ~375× |

There is ample headroom; the provider will not throttle these runs. This is worth recording because it was *not* always true: on the free tier (15 RPM / 250K TPM / 500 RPD) the pipeline's configured throughput exceeded the provider's RPM by ~4×, which would have manifested as a 429 storm → exhausted SQS retries → DLQ'd documents → paged alarms. That mismatch surfaced a latent design coupling we record as a finding below; Tier 1 removes it as a blocker for *this* exercise.

> [!NOTE]
> The `maximum_concurrency` cap is implicitly coupled to the provider's RPM budget: a cap that lets the pipeline issue more RPM than the tier allows turns a burst into DLQ'd documents, not buffered ones. At Tier 1 (4,000 RPM) the staging cap (10 → ~60 RPM) and even the prod cap (25 → ~150 RPM) sit far under the ceiling, so the coupling is currently slack. It is not enforced anywhere in code or config—see Finding 1.

## Decision

### Scope: end-to-end, through the real front door

Test the full path a real caller drives—**presigner (SigV4) → presigned PUT → S3 → EventBridge → SQS → extractor → DynamoDB → Streams → publisher → analytics S3**—not a shortcut that `put_object`s directly into the ingestion bucket. The goal is ecological validity ("how does the system behave on a real day"), so the front door is in scope even though it is not expected to be the bottleneck (API Gateway's default account throttle of 5,000 RPS dwarfs our draw).

Run against **staging**, never prod. Staging's lower concurrency cap (10 vs. 25) makes it the *conservative* read—it shows more queueing than prod would—and prod carries deletion protection and the live alarm fan-out. Where a prod number differs materially, the report annotates it (prod drains ~2.5× faster).

### Two scenarios, framed as a bracket

| Scenario | Definition | What it characterizes |
|---|---|---|
| **Sustained** | 200 documents at a steady ~0.22 doc/s over 15 minutes | The calm-normal-day baseline. At ~22% of staging capacity the queue never builds; this validates the warm-path happy case and steady-state latency. |
| **Burst** | 200 documents uploaded as fast as the client can presign + PUT them (parallel) | The spike / batch-dump worst case. Queue fills near-instantly; this is the real stressor—it exercises concurrency capping, queue depth, visibility-timeout safety, and DLQ behavior under saturation. |

These are deliberately a **bracket, not two stress tests**. Burst sits *above* instantaneous capacity (the queue must absorb); sustained sits *below* steady-state capacity (the queue stays near-empty). Together they bound "a normal day plus a bad moment." The sustained run is boring by design—it is the floor, and confirming it is uneventful *is* the result. We explicitly accept that sustained-at-200/15min does not find a limit; finding the limit (a ramp-to-knee test) is out of scope here and recorded as a follow-up.

### Corpus: a fixed, seeded sample from the Kleister NDA train partition

Sample 200 documents from the **Kleister NDA train partition**—materialized via the [`kleister-nda-preparation`](https://github.com/gafnts/kleister-nda-preparation) package—and reuse that exact sample, in the same order, across both scenarios. Each upload still mints a fresh `document_id`, so the runs produce 200 distinct extractions with no idempotency collapse.

Three reasons this beats a single held-constant document:

- **It matches the goal.** The objective is "how does the system behave on a normal day," and a normal day is documents of *varying* size and complexity. A real corpus produces a realistic latency and token distribution; the spread becomes a measured output (Layers B/C), not a fixed point a single document can never show.
- **Continuity with the quality benchmark.** ADR-0001's F1/cost/latency numbers (~91.5% / ~$0.007 / <10s) were measured on this same Kleister corpus, so the load test's per-document economics are directly comparable to the benchmark's rather than a separate regime.
- **Train, not dev/test, on purpose.** The benchmark uses the dev partition; reserving dev and test for quality evaluation and drawing load from train keeps the eval partitions uncontaminated as a measurement surface. Train is also larger than N=200, so we sample 200 *distinct* documents without replacement.

**Preserving the controlled-experiment property.** Varying the corpus *and* the arrival pattern at once would change two variables—the confound that made a single document tempting. The fix is to **freeze the sample with a fixed seed and use the identical 200 documents, in the same upload order, for both burst and sustained.** The corpus is then held constant *across* scenarios while varying *within* one: arrival pattern stays the only thing that differs between the two runs, and—better—each document can be paired across runs (same doc, burst vs. sustained) to isolate its queue-wait term cleanly.

**Sourcing.** The corpus is *not* committed—PDFs would bloat the repo and trip the `check for added large files` hook. A prep step fetches the train partition via the pinned `kleister-nda-preparation` package into a git-ignored directory under `tests/`, so runs are reproducible (pinned package + fixed seed) without versioning the documents. The realized token/size distribution is sanity-checked against the extractor's 120s timeout and the 4M Tier-1 TPM ceiling before a run—a corpus of unusually long NDAs is the one input that could approach either.

### What we measure

Three layers, mirroring the project's own observability split (CloudWatch operational telemetry vs. LangSmith LLM telemetry, per the README's Observability section) so the rig reads as an extension of the existing model rather than a bolt-on. A run is a *load test* only if it captures all three; capturing just traffic counts is "traffic generation."

#### Layer A—Pipeline dynamics (CloudWatch)

The operational plane under pressure, pulled with a single `GetMetricData` over the run window.

| Series | Source | What it answers |
|---|---|---|
| Queue depth & backlog age | SQS `ApproximateNumberOfMessagesVisible`, `ApproximateAgeOfOldestMessage` (main queue) | The shock-absorber's behavior—peak depth and max age are the burst's headline chart. |
| DLQ depth | `ApproximateNumberOfMessagesVisible` (both DLQs) | Must stay 0. Any message here is a failed run. |
| Redelivery | per-record `ApproximateReceiveCount` (already read as `attempt`, [handler.py:217](../../src/extractor/handler.py#L217)) | Confirms SLO 2: ≤ 1 per message → no premature visibility-timeout redelivery. |
| Extractor concurrency / duration / throttles | `AWS/Lambda` `ConcurrentExecutions`, `Duration` (p50/p90/p99), `Throttles`, `Invocations` | Does it pin at the cap (≤10)? Zero throttles expected—the SQS `maximum_concurrency` paces polling without emitting Lambda throttle errors. |
| Cold starts | `INIT_START` / `REPORT` `Init Duration` log lines | The container image pays a 3–10s cold start ([ADR-0009](0009-extractor-lambda.md)); count them and correlate to the latency outliers—expect a bimodal burst as the first ~10 envs warm. |
| Publisher lag | DynamoDB Streams `IteratorAge`, publisher `Duration`/`Errors` | Result delivery keeping up with the terminal-write fan-out. |
| Front door | API Gateway `Count`, `Latency`, `IntegrationLatency`, `4xx`/`5xx` | Captured to *prove* the presign path is a non-event (200 presigns ≈ 0.4% of the 5,000 RPS account throttle), not because signal is expected there. |
| DynamoDB | read/write throttles, `SuccessfulRequestLatency` | Should be zero on PAY_PER_REQUEST—but a never-loaded on-demand table taking 200 near-instant writes can brush the 2×-previous-peak adaptive ceiling, so the check is real, not a formality. |
| Alarm transitions | CloudWatch alarm history over the window | Did the alarms tell the truth? On a passing run, none fire (SLO 5). |

> [!NOTE]
> **Lambda `Errors` is not the failure signal.** The handler catches extraction exceptions and returns them as SQS `batchItemFailures` ([handler.py:309](../../src/extractor/handler.py#L309)), which Lambda counts as a *successful* invocation—so a document that exhausts its retries into the DLQ can leave `Errors = 0`. Logical success/failure is read from Layer B (terminal DynamoDB status), not here; `Errors` is retained only as an *unexpected-infra-fault* signal. See Finding 2.

#### Layer B—End-to-end (the user-facing number)

The SLO metric is upload→result-lands latency—but a single total hides *where* the time goes, and for the burst, where is the finding. The pipeline exposes clean server-side segment boundaries, so we decompose rather than measure one fuzzy total:

| Segment | Computed from | What it is |
|---|---|---|
| Queue wait | `created_at` (row, written at claim—[handler.py:159](../../src/extractor/handler.py#L159)) − upload `t0` | EventBridge→SQS→poll + queue dwell; the burst's dominant term. |
| Processing | `processing_ms` (row) | Pure `extract()` wall-clock, already isolated from queue wait. |
| Publish lag | S3 object landing − `completed_at` (row) | The publisher's 5s batch window—a *designed* lag, not a bottleneck. |
| **Total e2e** | sum; report p50 / p90 / p99 / max | The SLO number. |

Upload `t0` is stamped client-side; result-landing time comes from a **server-side** timestamp (the row's `completed_at` plus measured publish lag, or the S3 object itself)—**not** the poll-observed `LastModified`, which injects up to a full poll-interval of error and is second-granular (20–50% noise against a ~10s processing time). **Throughput** (docs/min completed, and how it plateaus at the cap) rounds out the layer.

#### Layer C—LLM economics

Already written to every row; the half that makes this a *KIE* pipeline rather than generic plumbing.

| Metric | Source | Note |
|---|---|---|
| Token usage (input/output) per doc | `token_usage` (row) | already recorded |
| Processing wall-clock per doc | `processing_ms` (row) | already recorded, queue-wait-isolated |
| Cost/doc → cost/1000 | tokens × Gemini pricing | the cost model |
| Schema-validation retries | LangSmith | if structured output fails validation and re-asks. **No agent tool calls**—the deployed strategy is `SinglePassExtractor`, one call, no ReAct loop ([ADR-0001](0001-event-driven-serverless-pipeline.md)) |

**The punchline, scoped honestly:** marginal cost and *steady-state* latency are ~entirely the LLM—the AWS data plane (Lambda GB-s, SQS, S3 PUTs, DynamoDB writes) is rounding error at this scale. But under *burst*, end-to-end latency is dominated by **queue wait**, which is the concurrency cap working as designed, not the LLM and not a bottleneck. Stating only the first half would contradict the burst chart.

### Pass/fail criteria (SLOs)

A run **passes** when:

1. **Correctness**—200/200 documents reach `succeeded`; zero `failed` rows; both DLQs stay at depth 0.
2. **No premature redelivery**—the burst drains well inside the 720s visibility timeout (predicted ~200s on staging) and no message is processed more than once (`ApproximateReceiveCount` ≤ 1 for every record).
3. **Concurrency cap holds**—peak `ConcurrentExecutions` ≤ 10 (staging); zero `Throttles` (the SQS event-source `maximum_concurrency` paces polling without emitting Lambda throttle errors—a prediction to confirm).
4. **Latency**—*processing* latency p90 within ~1.5× the benchmark (<~15s). *End-to-end* latency: sustained p90 < ~20s (incl. occasional cold start); burst tail bounded by queue position (~200–240s for the last doc on staging)—reported, not failed, since it is the designed buffering behavior.
5. **Alarms are honest**—on a *passing* run, no alarm fires (errors/throttles/DLQ all stay OK). A spurious alarm is itself a finding.

### Expected behavior (hypotheses to confirm or refute)

Stating predictions up front so the run tests them, rather than rationalizing whatever happens:

- **Burst:** queue peaks ~190–200, drains in ~3–4 min; concurrency pins at 10; latency is bimodal (first ~10 docs eat container cold start, rest warm); e2e latency is a near-linear ramp by upload order (queue position); DLQ stays 0; no alarm fires.
- **Sustained:** queue stays ≈0; concurrency hovers 1–3; latency ≈ processing latency (no queue wait); a few cold starts as idle envs reap and respawn; DLQ 0; no alarm fires.

If reality diverges from these, the divergence is the finding.

### The harness

A driver under `tests/load/`, marked `load` and deselected by default (mirroring how `integration` smoke tests are gated), reusing the terraform-output fixtures from [tests/conftest.py](../../tests/conftest.py):

- **Corpus prep**—a one-time `make`/fixture step fetches the Kleister NDA train partition via the pinned `kleister-nda-preparation` package into a git-ignored dir, then draws the seeded 200-document sample (fixed order) shared by both scenarios.
- **Injection**—burst fans presign+PUT of the sampled documents across a thread pool as fast as possible; sustained paces the same uploads at the target rate with light jitter over the window. Both record per-doc `document_id`, source filename, and upload-completion timestamp.
- **Completion tracking**—poll the analytics bucket (and/or DynamoDB with `ConsistentRead`) per `document_id` to *detect* landing, reusing the smoke test's poll pattern; but read the latency *segments* from server-side timestamps (`created_at`, `processing_ms`, `completed_at`, and the S3 object), not from the poll's wall-clock, per Layer B.
- **Metric collection**—after the window, a single CloudWatch `GetMetricData` pull for the Layer A series, a scan of the produced rows for the Layer B/C fields (`created_at`/`completed_at`/`processing_ms`/`token_usage`), and a read of alarm history over the window.
- **Reporting**—emit a JSON artifact + a printed summary table (percentiles, peak depth, drain time, cost, pass/fail per SLO).
- **Cleanup**—delete the run's ingestion objects, DynamoDB rows, and analytics objects in a `finally`, exactly as the smoke tests do.
- **Invocation**—`make load ENV=staging SCENARIO=burst|sustained`, refusing prod by the same guard `make smoke`/`apply` use.

## Consequences

Positive:

- First evidence of the pipeline's behavior under arrival pressure, against an explicit, pre-registered set of predictions and SLOs rather than after-the-fact graph-reading.
- Validates the operational plane end-to-end: that the queue buffers, the concurrency cap holds, the DLQ stays empty on a healthy run, and the alarms fire only when they should.
- The harness is reusable: re-runnable after any tuning change (concurrency cap, batch window, memory) to detect regressions, and extensible to the stubbed-LLM resilience suite and the ramp-to-knee test.
- Confirms or refutes the benchmark's latency/cost numbers in the *deployed* Lambda (incl. the cold-start tax), not just in isolation.

Negative:

- Real LLM spend (~$2.80 for both scenarios) and real writes to staging that must be cleaned up; a crashed run can leave orphaned objects/rows the cleanup must be robust against.
- The harness gains a corpus dependency: the `kleister-nda-preparation` package, a fetch/prep step, and a git-ignored corpus directory that must be materialized before a run. Reproducibility now rests on pinning the package version and the sample seed.
- The sustained scenario, by design, does not find a throughput limit; locating the knee needs the deferred ramp test.

Neutral:

- No production change. The harness is test-only; no `infra/` resource is added or modified to run it. The runs touch staging exactly as a real caller would.

## Findings

(Recorded as discovered; pre-implementation findings first.)

- **Finding 1—`maximum_concurrency` is implicitly coupled to the provider RPM budget, and nothing enforces it.** A cap that permits more RPM than the LLM tier allows converts a burst into DLQ'd documents (429 → exhausted SQS retries → DLQ → page), not buffered ones. Discovered while sizing this test against the free tier (15 RPM vs. the pipeline's ~60). Tier 1 makes the coupling slack today, so it is not a blocker—but the extractor has no 429-specific retry/backoff distinct from the generic SQS redrive, and the cap is not derived from any provider budget. Backlog: 429-aware backoff in the extractor, and/or documenting the cap-vs-RPM relationship at the root. Out of scope for this ADR; recorded so the next person sizing concurrency knows the constraint exists.

- **Finding 2 (hypothesis, to confirm during the run)—the `${extractor}-errors` alarm may not fire for a poison document the way the README's alarm table describes.** That table says a single bad document "fires this up to three times before it lands in the DLQ." But the handler catches extraction exceptions and reports them as SQS `batchItemFailures` ([handler.py:309](../../src/extractor/handler.py#L309)), which Lambda counts as *successful* invocations—so `Errors`, and therefore the `${extractor}-errors` alarm, may stay flat while the document retries into the DLQ, leaving only `${dlq}-messages-visible` to fire. The burst/sustained runs won't naturally produce failures (Tier 1 headroom), so this is a code-read hypothesis, not yet a finding; a deliberate poison-pill injection would confirm it. If confirmed, the README's alarm description needs a correction (the early-warning signal is the DLQ alarm, not the errors alarm).

## Alternatives considered

- **Stubbed / synthetic extractor (no LLM call).** The right tool for a *provider-independent resilience* test—it isolates infrastructure behavior with zero LLM cost or quota exposure, and would let us push far past 200. Rejected *for this exercise* because the stated goal is the behavior of the **real** system on a real day, and at N=200 the real-call cost is negligible (~200 × $0.007 ≈ $1.40/scenario, ~$2.80 for both, plus pennies of Lambda/DynamoDB/S3). Recorded as the intended approach for a future provider-independent resilience suite.
- **Direct `put_object` into the ingestion bucket (skip the presigner).** Simpler injection, and sufficient if only the extraction half were under test. Rejected: it omits the front door a real caller drives, undercutting the "real traffic" goal. The front door is cheap to include and worth validating.
- **Single document held constant (×200).** The cleanest controlled experiment—fixed document size makes arrival pattern the only variable, with zero sourcing. Rejected: it cannot produce a realistic latency/token distribution, so it answers "how does the pipeline behave" but not the stated "how does it behave on a normal day." The seeded-sample-reused-across-scenarios design (above) recovers most of that rigor—corpus constant across scenarios—while restoring ecological realism within each run.
- **Test prod for "real" numbers.** Rejected: staging is the safe, conservative read (lower cap → more visible queueing), and prod carries deletion protection and the live alarm fan-out. Prod deltas are annotated in the report instead.
- **Free tier, run as-is.** Rejected once Tier 1 was enabled. On the free tier the run would have measured Google's 429 limiter as much as our pipeline (see Finding 1); Tier 1 removes the artificial ceiling for ~the same per-token cost.
- **Ramp-to-knee (step load until the queue grows unboundedly).** The test that actually locates the throughput limit. Out of scope here—this ADR brackets normal-day behavior; the knee is a separate, follow-up exercise.
- **External load tool (k6 / Locust / Artillery).** Rejected for v1: the work is SigV4-signed presign + PUT + asynchronous result polling + CloudWatch metric correlation, which the existing Python/boto3 smoke fixtures already do most of. A Python driver reuses that machinery; an external tool would re-implement the signing and could not stamp e2e completion from the analytics bucket as cleanly.

# ADR-0016: Agentic-Flavor Deployment and Re-Parametrization

## Status

Proposed (2026-06-06).

## Context

The project is named `agentic-kie-deploy`, but every environment to date runs the **single-pass** extractor (`SinglePassExtractor`, [handler.py:84](../../src/extractor/handler.py#L84)). That was a deliberate, measured choice: the offline benchmark ([*When does agency earn its cost?*](https://gabriel.com.gt/blog/when-does-agency-earn-its-cost/)) found that on the Kleister NDA corpus single-pass dominates the matrix—~91.5% F1 at ~$0.007/doc and ~9.8s, while the agentic flavor cost 2–4× the latency and dollars (Claude-standard ran ~$0.038/~65s) for gains "insufficient to justify the overhead," and lite-tier agentic *regressed* more documents than it improved. Agency did not earn its cost, so we shipped the flavor that did.

That verdict is **offline**: a one-shot accuracy/cost eval on 83 dev documents. It says nothing about what agency costs *the deployed system under arrival pressure*—which is a different and harsher cost than per-document dollars. [ADR-0015](0015-load-testing-strategy.md) measured the deployed behavior of the single-pass flavor (both scenarios passed all five SLOs); the symmetric exercise for the agentic flavor has never been run. So three things are simultaneously true:

- The name promises a capability the deployment doesn't currently exercise.
- The strongest decision in the project—*not* shipping agentic—is only half-justified, because it rests on offline numbers and never confronts the deployed envelope.
- Deploying and load-testing the agentic flavor is where every dormant finding in ADR-0015 stops being hypothetical (the provider-RPM coupling of Finding 1; the errors-alarm-vs-DLQ-alarm question of Finding 2).

This ADR settles **how** the agentic flavor is deployed and, more importantly, how the architecture is *re-derived* for it—because the single-pass parameters are correct only for a ~10s, one-LLM-call-per-document workload, and the agentic flavor invalidates every input to that model.

### The agentic flavor changes the workload model, not just a constant

`AgenticExtractor` builds a LangChain ReAct agent that explores the PDF via tools (`get_page_count`, `read_text`, optionally `load_images`) and stops when it has enough information. Concretely, versus single-pass:

| Property | Single-pass | Agentic | Consequence |
|---|---|---|---|
| LLM calls per document | exactly 1 | N, data-dependent (1 → `max_iterations`) | request rate decouples from document rate |
| Service time | ~10s (p99 31s) | ~25–40s expected (2–4×), fatter/bimodal tail | steady-state capacity collapses |
| Input tokens/doc | fixed per document | inflated (re-reads pages across turns) | provider TPM headroom shrinks |
| Failure modes | one call succeeds/fails | loop non-termination, repeated tool error, partial state | `max_iterations` exhaustion → `ExtractionError` |

The single-pass parameters were *derived* from its workload model (service ~10s → capacity `cap ÷ service` ≈ 60/min at staging; provider draw = throughput because calls = throughput). Re-using those constants for agentic isn't conservative—it's mis-tuned. The honest move is to re-run the derivation, the same way ADR-0015 wrote a model and graded it.

### The architectural change: one concurrency knob becomes two

In single-pass, the SQS event-source `maximum_concurrency` ([extractor/main.tf:130](../../infra/modules/extractor/main.tf#L130)) does three jobs at once *because one document equals one LLM call*: it caps document parallelism (throughput), caps concurrent LLM requests (the cost-burst guardrail), and bounds the provider RPM draw (Finding 1's coupling). Those collapse into a single number only at a 1:1 doc-to-call ratio.

Agentic fans out **inside** a document, so documents-in-flight ≠ requests-in-flight: the request side now scales with `cap × calls_per_doc`, which is variable and which the SQS cap does not control. The cap still governs throughput, but the cost-guardrail and provider-coupling jobs need a **second control surface**: a request-level limiter (token bucket / semaphore in the handler) sized against the Gemini RPM/TPM budget. The SQS event-source cap governs *document* parallelism; the in-handler limiter governs *request* parallelism. That decoupling is the real architectural finding—the deployed-infra echo of the offline thesis: agency doesn't merely cost more per document, it breaks the assumption that one knob controls both throughput and provider exposure.

## Decision

### Flavor is a deploy-time parameter, single-pass stays the default

Introduce `var.extractor_flavor` (`single_pass` | `agentic`, default `single_pass`). It drives two things:

1. **The handler constructor.** `_extractor()` ([handler.py:84-90](../../src/extractor/handler.py#L84-L90)) reads a new `EXTRACTOR_FLAVOR` env var and builds either `SinglePassExtractor(model, schema)` (today) or `AgenticExtractor(model, schema, modality="text", max_iterations=<profile>)`. Both are already exported by `agentic_kie`, share the identical `(model, schema)` interface, and raise the same `ExtractionError` the handler already catches into `batchItemFailures` ([handler.py:356](../../src/extractor/handler.py#L356))—so the agentic failure path flows through the existing redrive/DLQ machinery unchanged. `Extractor[NDA]` (also exported) becomes the return type so the cache helper covers both.
2. **The parameter profile** (below), so the infra constants move *with* the flavor rather than being hand-edited per run.

Prod is untouched—it remains single-pass with deletion protection. The agentic profile is applied to **staging** for the characterization run (staging's single-pass baseline already lives in the ADR-0015 artifacts, so re-applying it loses nothing), then reverted. A dedicated `staging-agentic` environment is the cleaner-but-heavier alternative (recorded below).

### The re-derived parameter profile

| Knob | Single-pass (today) | Agentic profile | Why it moves |
|---|---|---|---|
| `max_iterations` (agent) | n/a | **8–12** (down from the library default 50) | The real cost/latency governor. A doc that can't terminate should fail fast into a *bounded* cost, not burn 50 LLM calls. This is the agentic analog of single-pass's deterministic single call. |
| `modality` | `text` | `text` | Avoids image-token blow-up; keeps the per-doc TPM draw bounded and Finding 1's coupling slack. |
| Lambda timeout ([main.tf:33](../../infra/main.tf#L33)) | 120s | **300s** (backstop) | Above the worst legitimate `max_iterations`-bounded run (~10 calls), not the governor. A timeout is a crash → retry → wasted spend; `max_iterations` should bite first. |
| Visibility timeout | 720s (= 120×6) | **1800s** (= 300×6, automatic) | Already *derived* as `timeout × 6` ([queue/main.tf:2](../../infra/modules/queue/main.tf#L2)). Raising the Lambda timeout moves it in lockstep—one knob, not two—and is exactly what keeps SLO 2 from breaching under the longer queue dwell. |
| `maximum_concurrency` | 10 staging / 25 prod | **10** (held — cost-preserving) | See the fork below. |
| **(new) request-level limiter** | implicit in the cap | explicit token bucket vs RPM/TPM | in-doc fan-out decoupled it from the cap (see Context). |
| `maxReceiveCount` ([queue default](../../infra/modules/queue/variables.tf#L16)) | 3 | **2** | Agentic failures are mostly logic (non-terminating loop, repeated tool error), not transient. Retrying an expensive doomed run 3× triples its cost for nothing. |
| `batch_size` / batching window | 1 / 0 | 1 / 0 (unchanged) | One long ReAct run per invocation is already correct; batching would head-of-line-block. |
| Memory | 2048 MB | 2048 MB (revisit) | Latency is LLM-wall-clock-bound (network), not CPU-bound; memory buys cold-start and glue speed only. A modest lever, left at baseline pending evidence. |

**The downstream half does not move.** The publisher (DynamoDB Streams → analytics S3, 5s batch window) is flavor-agnostic—it runs *after* extraction and neither knows nor cares which extractor wrote the row. The re-derivation is entirely the extractor, its event source, and the provider budget.

### The one genuine fork: throughput vs. cost containment

Capacity is `cap ÷ service_time`. To hold single-pass-like drain behavior (a 200-burst absorbed and drained in a few minutes) the cap would rise from 10 to ~30 to offset the ~3× longer service time. That fights the cost guardrail. The choice:

- **Throughput-preserving**: raise the cap to ~30, keep drains fast, accept a ~3× wider cost-burst exposure on the *expensive* flavor.
- **Cost-preserving** (chosen): hold the cap at 10, let SQS hold the backlog longer, and pay for the longer dwell with the higher (auto-derived) visibility timeout.

**We choose cost-preserving.** Agentic is the flavor that already doesn't earn its cost; letting it *also* fan out 30-wide and spike spend is the wrong instinct. Lean harder on the buffer the architecture already has, not on the throttle. That stance is itself the finding: *the right response to a slower, costlier workload is to widen the buffer's job, not the throttle's.* (Flip this one knob and the rest of the profile is unchanged—the decision is isolated by design.)

## Pass/fail criteria (SLOs)

The agentic runs reuse ADR-0015's five SLOs, adjusted for the re-derived envelope; criterion 6 is new and is the point of the exercise.

1. **Correctness (primary run)**—200/200 reach `succeeded`; both DLQs at 0. (A *deliberate low-`max_iterations` stressor run* is exempt and expected to DLQ—see criterion 5.)
2. **No premature redelivery**—`ApproximateAgeOfOldestMessage` stays well under the **new 1800s** visibility timeout and the queue drains to empty. This is the SLO the re-parametrization exists to protect: under the *old* 720s timeout, a 200-burst at ~30s service would push the last messages to ~570s dwell and brush redelivery. Confirming it holds under the new profile—and would not under the old—is the headline.
3. **Concurrency & provider rate hold**—peak `ConcurrentExecutions` ≤ cap; zero `Throttles`; **and** the in-handler limiter keeps the LLM request rate under the Gemini RPM/TPM budget (the new control surface working).
4. **Latency—reported, not gated, and compared.** Agentic is slow by design; the e2e/processing percentiles are reported, not failed. The *deliverable* is the agentic-vs-single-pass delta on the same corpus in the same deployed pipeline (criterion 6).
5. **Alarms honest**—primary run: no alarm fires. **Stressor run: this finally exercises Finding 2.** When `max_iterations` is capped low enough that genuinely hard docs exhaust it → `ExtractionError` → retry → DLQ, the prediction (from [handler.py:356](../../src/extractor/handler.py#L356)) is that `Errors` stays flat (failures are reported as `batchItemFailures`, a *successful* invocation) and **only** the `${dlq}-messages-visible` alarm fires, not `${extractor}-errors`. Confirming this on a live run closes Finding 2.
6. **The deployed agency premium (new)**—cost/doc and e2e-latency, agentic vs. single-pass, measured not benchmarked: the offline "agency doesn't earn its cost" verdict, plus the infra cost the benchmark never saw (slower drain, the retune this ADR documents).

## Expected behavior (hypotheses to confirm or refute)

- **Service time** ~25–40s mean (2–4× single-pass), tail bounded by `max_iterations` rather than by a 120s crash; **capacity** collapses from ~60/min to ~15–25/min at the held cap.
- **Burst**: queue peaks near 200 (as single-pass), but *drains in ~8–13 min* not ~4; concurrency pins at the cap; oldest-message age peaks ~400–600s—comfortably under 1800s, **breaching the old 720s**. DLQ 0 on the primary run; no alarm.
- **Sustained**: at a rate set to ~22% of the *new* capacity, queue ≈ 0, concurrency hovers low; latency ≈ processing (which is now multi-call and several-fold higher).
- **Cost**: ~$0.015–0.025/doc on Gemini text-modality agentic (more calls, but no image tokens, cheaper model than the blog's Claude-standard); ~$6–10 for both scenarios.
- **Finding 2 stressor**: docs that exhaust the low `max_iterations` DLQ cleanly with `Errors` flat and only the DLQ alarm firing.

If reality diverges, the divergence is the finding.

## The harness

No new harness. The ADR-0015 driver under `tests/load/` is **flavor-agnostic**: it presigns + PUTs documents, polls for landing, and reads server-side `created_at` / `processing_ms` / `completed_at` / `token_usage` plus the Layer A CloudWatch series and alarm history. None of that is single-pass-specific. So the existing `make load ENV=staging SCENARIO=burst|sustained` runs against the agentic deployment unchanged; the only difference is which flavor profile staging was applied with. The agentic artifacts land alongside the single-pass baseline in `tests/load/reports/`, and the per-document pairing (same corpus, same upload order) extends to a third axis—single-pass vs agentic on the identical document.

## Consequences

Positive:

- The project earns its name: it deploys `agentic-kie`, both flavors, selected at deploy time.
- The offline "agency doesn't earn its cost" verdict gains its deployed counterpart, including the infra cost the benchmark could not measure.
- Findings 1 and 2 move from hypotheses to live results; the request-level limiter and the cap-decoupling are exercised, not just reasoned about.
- The re-parametrization is reusable: the flavor profile is the template for any future heavier workload (multimodal, a larger schema).

Negative:

- Real work: a handler constructor switch, a new `extractor_flavor` parameter + profile plumbing, and the request-level limiter (genuinely new code, not a config change). More LLM spend (~$6–10) than the single-pass runs.
- Re-applying staging to the agentic profile displaces its single-pass deployment for the duration (mitigated: the baseline is already captured; or stand up `staging-agentic`).
- The agentic flavor does not change the production decision—single-pass remains the default. This is characterization, not a reversal.

Neutral:

- Prod is untouched. The agentic profile is staging-only and reverted after the run.

## Findings

(Recorded as discovered; pre-implementation findings first.)

- **Finding A—`max_iterations` defaults to 50, which is a latency/cost bomb in a Lambda.** The library default lets a single document drive up to 50 LLM calls before raising. Under a 120s function timeout that document would crash (timeout) long before iteration 50, turning a logic problem into an infra fault and a retry. The profile caps it at 8–12 so the *agent* governs cost, and raises the timeout so the cap—not the clock—is what bites. The single-pass flavor never surfaced this because it has no loop.
- **Finding B (to confirm)—the SQS event-source cap stops being a provider-rate control under agentic.** Because in-doc fan-out decouples request rate from document rate, holding `maximum_concurrency` no longer bounds RPM/TPM. Whether the new in-handler limiter is necessary, or Tier 1's headroom absorbs `cap × calls_per_doc` anyway, is a quantity to measure on the run, not assume.

## Alternatives considered

- **Flip the existing staging extractor by env var only (no parameter profile).** Simplest, but re-parametrizing (timeout → visibility, `maxReceiveCount`, the limiter) means editing shared infra by hand per run, and you cannot hold a clean single-pass baseline alongside. Rejected: the flavor and its derived envelope should move together as one parameter.
- **Throughput-preserving cap (~30).** Holds single-pass drain times. Rejected for v1 (see the fork): it widens cost exposure on the flavor we deploy *because* it's expensive. Recorded as a one-line flip if drain time ever matters more than spend.
- **Multimodal / image modality.** Closer to what a "read the document like a human" agent implies, and what some benchmark rows used. Rejected for the deploy: image tokens multiply the TPM draw and re-tighten Finding 1's coupling for no measured accuracy win on this text-heavy NDA corpus. `text` keeps the provider budget slack.
- **Dedicated `staging-agentic` environment.** A true side-by-side: agentic and single-pass live simultaneously, no baseline displacement. Heavier (a full env stand-up, its own alarms, its own teardown) and unnecessary given the baseline is already captured. Recorded as the cleaner path if a *continuous* A/B is ever wanted, per the single-tenant deployment model ([ADR-0013](0013-single-tenant-deployment-model.md)).
- **Don't deploy agentic; explain the name in prose.** The zero-cost path: a README/blog line saying the name refers to the library, which implements both flavors. Rejected as the anticlimactic answer—it leaves the project's strongest decision resting on offline numbers and forgoes the most interesting load-testing exercise available.

## Post-implementation

(To be completed after the runs, mirroring ADR-0015: the hypotheses above graded against the artifacts, the deployed agency premium reported, and Findings 1/2/A/B resolved or carried.)

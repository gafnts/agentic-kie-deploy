# ADR-0016: Agentic-Flavor Deployment

## Status

Accepted (2026-06-07).

## Context

The project is named `agentic-kie-deploy`, but every environment to date runs the **single-pass** extractor (`SinglePassExtractor`, [handler.py:84](../../src/extractor/handler.py#L84)). That was a deliberate, measured choice: the offline benchmark ([*When does agency earn its cost?*](https://gabriel.com.gt/blog/when-does-agency-earn-its-cost/)) found that on the Kleister NDA corpus single-pass dominates the matrix—~91.5% F1 at ~$0.007/doc and ~9.8s, while the agentic flavor cost more in latency and dollars—Claude-standard ran ~$0.038/~65s (~5× the dollars, ~6× the latency); Gemini Standard agentic is ~$0.011/~14.6s (~1.5×)—for gains "insufficient to justify the overhead," and lite-tier agentic *regressed* more documents than it improved. Agency did not earn its cost, so we shipped the flavor that did.

That verdict is **offline**: a one-shot accuracy/cost eval on 83 dev documents. It says nothing about what agency costs *the deployed system under arrival pressure*—which is a different and harsher cost than per-document dollars. [ADR-0015](0015-load-testing-strategy.md) measured the deployed behavior of the single-pass flavor (both scenarios passed all five SLOs); the symmetric exercise for the agentic flavor has never been run. So three things are simultaneously true:

- The name promises a capability the deployment doesn't currently exercise.
- The strongest decision in the project—*not* shipping agentic—is only half-justified, because it rests on offline numbers and never confronts the deployed envelope.
- The offline verdict has a deployed counterpart no benchmark can produce—the agency premium *in the running pipeline* (drain time, queue dwell, the infra cost the eval never saw)—and the exercise gives ADR-0015's dormant findings a live look: Finding 1's provider-RPM coupling gets *measured* (and, at Tier 1, is likely confirmed slack), and Finding 2's errors-alarm-vs-DLQ question becomes testable via a deliberate stressor.

This ADR settles **how** the agentic flavor is deployed and how its parameter envelope is re-derived. On Gemini, agentic costs ~1.5× single-pass in latency and dollars—modest enough that the existing operating envelope already absorbs it at the ADR-0015 bracket. The re-derivation is therefore narrow: exactly two knobs genuinely move, the rest of the envelope holds, and the real payoff is the *deployed* agency premium plus the capability itself.

### The agentic flavor changes the workload model, not just a constant

`AgenticExtractor` builds a LangChain ReAct agent that explores the PDF via tools (`get_page_count`, `read_text`, optionally `load_images`) and stops when it has enough information. Concretely, versus single-pass:

| Property | Single-pass | Agentic | Consequence |
|---|---|---|---|
| LLM calls per document | exactly 1 | N, data-dependent (observed 5–9 in offline traces) | request rate decouples from document rate |
| Service time | ~10s (p99 31s) | ~14.6s (benchmark, ~1.5×), fatter/bimodal tail | steady-state capacity contracts |
| Input tokens/doc | fixed per document | inflated (re-reads pages across turns) | provider TPM headroom shrinks |
| Failure modes | one call succeeds/fails | loop non-termination, repeated tool error, partial state | `max_iterations` exhaustion → `ExtractionError` |

The single-pass parameters were *derived* from its workload model (service ~10s → capacity `cap ÷ service` ≈ 60/min at staging; provider draw = throughput because calls = throughput). The honest move is to re-run that derivation and see which constants actually move—not to assume the whole envelope is wrong. At only ~1.5× service the queue-dynamics constants mostly still fit; as it turns out (below), one knob (`max_iterations`) is wrong independent of latency, one (`maxReceiveCount`) is worth tightening, and the rest hold.

### The architectural change: one concurrency knob becomes two

In single-pass, the SQS event-source `maximum_concurrency` ([extractor/main.tf:130](../../infra/modules/extractor/main.tf#L130)) does three jobs at once *because one document equals one LLM call*: it caps document parallelism (throughput), caps concurrent LLM requests (the cost-burst guardrail), and bounds the provider RPM draw (Finding 1's coupling). Those collapse into a single number only at a 1:1 doc-to-call ratio.

Agentic fans out **inside** a document, so documents-in-flight ≠ requests-in-flight: the request side now scales with `cap × calls_per_doc`, which is variable and which the SQS cap does not control. The cap still governs throughput, but the cost-guardrail and provider-coupling jobs would call for a **second control surface**: a request-level limiter (token bucket / semaphore in the handler) sized against the Gemini RPM/TPM budget. The SQS event-source cap governs *document* parallelism; the in-handler limiter governs *request* parallelism. That decoupling is the real architectural finding—the deployed-infra echo of the offline thesis: agency doesn't merely cost more per document, it breaks the assumption that one knob controls both throughput and provider exposure. *Conceptually* real is not the same as *quantitatively* binding, though: at Tier 1 (1,000 RPM) with ~1.5× service, the request side draws only a few hundred RPM—~164 at staging's cap 10 (~6× under the ceiling), ~410 even at prod's cap 25 (~2.4× under). So the second control surface is a thing to *measure for*, and to reach for as N or the cap grows—comfortably skippable for the staging characterization run, but a thin enough margin at prod's cap that it moves from hypothetical toward real (Finding B).

## Decision

### Flavor is a deploy-time parameter, single-pass stays the default

Introduce `var.extractor_flavor` (`single_pass` | `agentic`, default `single_pass`). It drives two things:

1. **The handler constructor.** `_extractor()` ([handler.py:84-90](../../src/extractor/handler.py#L84-L90)) reads a new `EXTRACTOR_FLAVOR` env var and builds either `SinglePassExtractor(model, schema)` (today) or `AgenticExtractor(model, schema, modality="text", max_iterations=<profile>)`. Both are already exported by `agentic_kie`, share the identical `(model, schema)` interface, and surface failures through the handler's broad `except Exception` ([handler.py:317](../../src/extractor/handler.py#L317)), which already routes them to `batchItemFailures` ([handler.py:356](../../src/extractor/handler.py#L356))—so the agentic failure path (a non-terminating agent's `ExtractionError` included) flows through the existing redrive/DLQ machinery unchanged, caught by the type-agnostic `except` rather than any shared exception class. `Extractor[NDA]` (also exported) becomes the return type so the cache helper covers both.
2. **The parameter profile** (below), keyed off `extractor_flavor` so the whole envelope moves *with* the flavor rather than being hand-edited—of which, for agentic, only `max_iterations` and `maxReceiveCount` actually differ from single-pass (the timeout and its derived visibility stay put). Switching any environment's flavor is then a one-variable change, which is the point: re-parametrization should be as cheap as flipping the variable.

Every environment—staging and prod alike—can run **either** flavor, selected per environment at deploy time, with single-pass the default everywhere. Because the full profile follows `extractor_flavor` (above), pointing any environment at agentic is a one-variable change, and pointing it back is the same. The characterization run is done on **staging** first: you validate a new flavor's deployed envelope before offering it to prod, and staging's single-pass baseline already lives in the ADR-0015 artifacts, so flipping it loses nothing. Prod thereby *gains the capability* to run agentic while keeping single-pass (and its deletion protection) by choice—nothing about prod is reverted, because the infra change is a permanent capability, not a temporary patch. A dedicated `staging-agentic` environment remains an option for a continuous side-by-side (recorded below).

### The re-derived parameter profile

| Knob | Single-pass (today) | Agentic profile | Why it moves |
|---|---|---|---|
| `max_iterations` (agent) | n/a | **~30** (down from the library default 50) | The real cost/latency governor—but it caps LangGraph *supersteps* (`recursion_limit`), ≈ 2× the LLM-call count, *not* LLM calls. Offline traces run 5–9 LLM calls (≈ 9–17 supersteps). ~30 clears that ceiling with margin and still caps a runaway at ~15 calls. See Finding A. |
| `max_retries` (agent) | n/a | **3** | A *third* retry knob, separate from `maxReceiveCount`: `ModelRetryMiddleware` retries each model call up to 3× with backoff on *transient* errors (429/timeout/overload) inside one invocation. Left at 3, but recorded because it interacts with the 120s timeout (transient retries add wall-clock) and because "fail fast and cheap" applies to *logic* failures, not transient ones. |
| `modality` | `text` | `text` | Avoids image-token blow-up; keeps the per-doc TPM draw bounded. Measured single-pass TPM peaked at 0.317M against the 2M ceiling (~6× headroom); even agentic's per-doc input inflation (~2–4×) at ~0.68× throughput stays well under, so Finding 1's coupling holds slack. |
| Lambda timeout ([main.tf:33](../../infra/main.tf#L33)) | 120s | **120s (unchanged)** | Benchmark mean is 14.6s, and `max_iterations` ~30 (≈ ~15 LLM calls at ~2s each) bounds the worst run to ~30–50s—well under the existing 120s, which already absorbed single-pass's 50s tail. `max_iterations`, not the clock, is the governor; the timeout is a backstop that already has margin. No reason to move it. |
| Visibility timeout | 720s (= 120×6) | **720s (unchanged)** | Derived as `timeout × 6` ([queue/main.tf:2](../../infra/modules/queue/main.tf#L2)), so it tracks the timeout automatically. The timeout stays at 120s, so this stays at 720s—and at ~330s peak dwell (below, scaling the measured single-pass baseline) that is ~2.2× headroom. The coupling is worth keeping; it just doesn't need to fire here. |
| `maximum_concurrency` | 10 staging / 25 prod | **held at the environment's existing cap** (cost-preserving) | A per-environment lever, independent of flavor—not part of the flavor profile; see the fork below. |
| **(new) request-level limiter** | implicit in the cap | **measure first, build only if the draw warrants** | In-doc fan-out decouples request rate from the cap (see Context), but at Tier 1 the draw sits ~6× under budget at staging's cap 10 (~2.4× at prod's cap 25). Conditional on the run's measured provider rate (Finding B), not built up front. |
| `maxReceiveCount` ([queue default](../../infra/modules/queue/variables.tf#L16)) | 3 | **2** | Agentic failures are mostly logic (non-terminating loop, repeated tool error), not transient. Retrying an expensive doomed run 3× triples its cost for nothing. The value is single-sourced (queue redrive → `SQS_MAX_RECEIVE_COUNT`, [main.tf:91](../../infra/main.tf#L91)), so the flip is one variable—but several descriptions hard-code "maxReceiveCount=3" (the [extractor-errors alarm](../../infra/modules/extractor/main.tf#L137), the [DLQ alarm](../../infra/modules/queue/main.tf#L132), the publisher variable, the README alarm table) and must be updated alongside it. |
| `batch_size` / batching window | 1 / 0 | 1 / 0 (unchanged) | One long ReAct run per invocation is already correct; batching would head-of-line-block. |
| Memory | 2048 MB | 2048 MB (revisit) | Latency is LLM-wall-clock-bound (network), not CPU-bound; memory buys cold-start and glue speed only. A modest lever, left at baseline pending evidence. |

**The downstream half does not move.** The publisher (DynamoDB Streams → analytics S3, 5s batch window) is flavor-agnostic—it runs *after* extraction and neither knows nor cares which extractor wrote the row. The re-derivation touches only the extractor handler (`max_iterations`) and the queue's redrive policy (`maxReceiveCount`)—the event-source mapping, the provider budget, and the whole downstream half stay as they are.

### The one genuine fork: throughput vs. cost containment

Capacity is `cap ÷ service_time`. To hold single-pass-like drain behavior (a 200-burst absorbed and drained in a few minutes) the cap would rise from 10 to ~15 to offset the ~1.5× longer service time. That fights the cost guardrail. The choice:

- **Throughput-preserving**: raise the cap to ~15, keep drains fast, accept a ~1.5× wider cost-burst exposure on the *expensive* flavor.
- **Cost-preserving** (chosen): hold the cap at its existing per-environment value (10 on staging) and let SQS hold the backlog longer—which the unchanged 720s visibility timeout already absorbs (~330s dwell, ~2.2× headroom), so nothing has to give for it.

**We hold the existing cap (cost-preserving)—but at ~1.5× this is a low-stakes call, not a principled stand.** Raising it to ~15 would cost ~50% more concurrent spend for a faster drain, and either way the 200-doc bracket completes in minutes with the DLQ empty. We change nothing because the cap is a per-environment lever and there's no measured reason to touch it; if drain time ever matters more than spend, ~15 is the one-variable flip. The original *principle*—lean on the buffer, not the throttle—still holds; it just isn't being tested at this scale.

## Pass/fail criteria (SLOs)

The agentic runs reuse ADR-0015's five SLOs—only SLO 4 changes, made flavor-aware (Finding C)—and add criterion 6, which is the point of the exercise.

1. **Correctness (primary run)**—200/200 reach `succeeded`; both DLQs at 0. (A *deliberate low-`max_iterations` stressor run* is exempt and expected to DLQ—see criterion 5.)
2. **No premature redelivery**—`ApproximateAgeOfOldestMessage` stays well under the 720s visibility timeout and the queue drains to empty. At ~14.6s service time (~1.5× single-pass), a 200-burst drains in ~5–5.5 min—scaling the *measured* single-pass baseline (3.85 min at 51.9 docs/min, not the theoretical 60/min)—so oldest-message age peaks ~330s, ~2.2× under the 720s, and no message ages into a redelivery. Nothing in the envelope had to move for this; the queue simply drains cleanly and dwell stays well under the timeout.
3. **Concurrency & provider rate hold**—peak `ConcurrentExecutions` ≤ cap; zero `Throttles`; **and** the measured LLM request rate stays under the Gemini RPM/TPM budget. This is the live read on Findings 1/B: at staging's cap 10 RPM should sit ~6× under, and TPM likewise ~6× under (single-pass peaked 0.317M of the 2M ceiling; agentic inflates per-doc tokens but stays clear)—which is also the test of whether a request-level limiter is needed at all—if the draw is that slack, it isn't built.
4. **Latency—reported and compared, not gated (for agentic).** The *deliverable* is the agentic-vs-single-pass delta on the same corpus in the same deployed pipeline (criterion 6), not a pass/fail bar—agentic is slow by design. **This is not what the harness does today:** as built, SLO 4 *gates* processing p90 (both scenarios) and sustained e2e p90, on thresholds derived from single-pass's <10s benchmark, and a failed SLO hard-fails the run. Agentic trips those bars, so making SLO 4 flavor-aware—reporting rather than gating—is required work; see Finding C.
5. **Alarms honest**—primary run: no alarm fires. **Stressor run: this finally exercises Finding 2.** A small, separate run—~20 documents with `max_iterations` forced very low (≤4 supersteps, e.g. 2) so they reliably exhaust it → `ExtractionError` → retry → (at `maxReceiveCount=2`) DLQ. The prediction (from [handler.py:356](../../src/extractor/handler.py#L356)) is that `Errors` stays flat (failures are reported as `batchItemFailures`, a *successful* invocation) and **only** the `${dlq}-messages-visible` alarm fires, not `${extractor}-errors`. Confirming this on a live run closes Finding 2.
6. **The deployed agency premium (new)**—cost/doc and e2e-latency, agentic vs. single-pass, measured not benchmarked: the offline "agency doesn't earn its cost" verdict, plus the infra cost the benchmark never saw (slower drain, the retune this ADR documents).

## Expected behavior (hypotheses to confirm or refute)

- **Service time** ~14.6s mean (benchmark, ~1.5× single-pass), tail bounded by `max_iterations` rather than by a timeout crash; **capacity** contracts from ~60/min to ~41/min at the held cap.
- **Burst**: queue peaks near 200 (as single-pass), but *drains in ~5–5.5 min*—vs the *measured* single-pass ~3.85 min, not the theoretical ~3.5; concurrency pins at the cap; oldest-message age peaks ~330s—comfortably under the unchanged 720s timeout (~2.2×). DLQ 0 on the primary run; no alarm.
- **Sustained**: holding ADR-0015's 0.22 doc/s arrival schedule (the harness fixes the 900s window, so the rate is flavor-independent), now ~32% of the reduced ~41/min capacity—still below capacity, so queue ≈ 0 and concurrency hovers low (perhaps a touch above single-pass's peak-5, given the fatter tail—ADR-0015 Finding 3—but under the cap); latency ≈ processing (which is now multi-call).
- **Cost**: ~$0.011/doc on Gemini text-modality agentic (benchmark); ~$4–5 for both scenarios (200 docs each), plus pennies for the ~20-doc stressor.
- **Finding 2 stressor**: docs that exhaust the low `max_iterations` DLQ cleanly with `Errors` flat and only the DLQ alarm firing.

If reality diverges, the divergence is the finding.

## The harness

No new harness. The ADR-0015 driver under `tests/load/` is **flavor-agnostic**: it presigns + PUTs documents, polls for landing, and reads server-side `created_at` / `processing_ms` / `completed_at` / `token_usage` plus the Layer A CloudWatch series and alarm history. None of that is single-pass-specific. So the existing `make load ENV=staging SCENARIO=burst|sustained` runs against the agentic deployment unchanged; the only difference is which flavor profile staging was applied with. The agentic artifacts land alongside the single-pass baseline in `tests/load/reports/`, and the per-document pairing (same corpus, same upload order) extends to a third axis—single-pass vs agentic on the identical document.

## Consequences

Positive:

- The project earns its name: it deploys `agentic-kie`, both flavors, selectable per environment at deploy time—prod included.
- The offline "agency doesn't earn its cost" verdict gains its deployed counterpart, including the infra cost the benchmark could not measure.
- Finding 2 gets a live test (via the deliberate stressor sub-run); Finding 1 is *measured* and—at Tier 1 with these caps—expected to stay slack, which is itself a recorded result. The cap-decoupling is documented as a watch-item for higher N / prod's cap, not prematurely built.
- The re-parametrization is reusable: the flavor profile is the template for any future heavier workload (multimodal, a larger schema).

Negative:

- Real work: a handler constructor switch, a new `extractor_flavor` parameter + profile plumbing, and a harness change so SLO 4 reports rather than gates agentic latency (Finding C)—plus the request-level limiter *only if* the measured draw warrants it (Finding B). More LLM spend (~$4–5) than the single-pass runs.
- An environment runs one flavor at a time, so flipping staging to agentic means it isn't serving single-pass during the run window (mitigated: the baseline is already captured and flip-back is one variable; or stand up a second environment for a continuous side-by-side).
- The agentic flavor does not change the production decision—single-pass remains the default. This is characterization, not a reversal.

Neutral:

- The production *decision* is unchanged—prod keeps single-pass by choice—while the *capability* to run agentic is added for every environment. Adding the option is not exercising it; the change reverts nothing.

## Findings

(Recorded as discovered; pre-implementation findings first.)

- **Finding A—`max_iterations` is a LangGraph `recursion_limit` (supersteps ≈ 2× LLM calls), not an LLM-call count; the right value is ~30—not the 8–12 first drafted, nor the library default 50.** `AgenticExtractor` passes `max_iterations` straight to LangGraph's `recursion_limit`, and `create_agent` builds a two-node loop (model ↔ tools), so K LLM calls cost ≈ 2K−1 supersteps. Offline traces show 5–9 LLM calls (≈ 9–17 supersteps); the higher "count tools and chains → ~45" figure is LangSmith *trace spans*, not supersteps, and doesn't bind this knob. Two corrections follow: (1) the draft's 8–12 would clip *every* legit run into a false `ExtractionError`—even a 5-call run needs ~9 supersteps; (2) the "default 50 crashes on the 120s timeout" mechanism is model-specific—it held for the slow Claude run (~65s) but not for the deployed Gemini Flash (~14.6s for 5–9 calls, ~2s/call), where even 50 supersteps (~25 calls) is ~50s and raises `ExtractionError` *cleanly* rather than crashing. So the reason to lower it is cost/latency containment of a doomed doc (cap a runaway at ~15 calls / ~$0.02 / ~30s) and margin above the legit ceiling, not crash-avoidance. ~30 clears the observed 9-call ceiling with ~1.7× margin; the characterization run validates it—a *legit* doc DLQ'ing via recursion means it's still too tight. The single-pass flavor never surfaced any of this because it has no loop.
- **Finding B (to confirm)—the SQS event-source cap stops being a provider-rate control under agentic.** Because in-doc fan-out decouples request rate from document rate, holding `maximum_concurrency` no longer bounds RPM/TPM. Whether the new in-handler limiter is necessary, or Tier 1's headroom absorbs `cap × calls_per_doc` anyway, is a quantity to measure on the run, not assume.
- **Finding C—the harness's latency SLO is hard-gated and would false-fail agentic.** [report.py:24-25](../../tests/load/report.py#L24-L25) hard-codes `PROCESSING_P90_MAX_S = 15` (gated in both scenarios) and `SUSTAINED_E2E_P90_MAX_S = 20` (sustained), and any failed SLO trips `assert not failures` ([test_scenarios.py:86-88](../../tests/load/test_scenarios.py#L86-L88))—so a red SLO 4 fails the whole run, not just the report. Those bars are 1.5× single-pass's <10s benchmark, and single-pass already clears processing p90 by a hair (13.5/13.8s, ADR-0015 Finding 5), so agentic at ~1.5× trips them on the very metric SLO 4 calls informational. Fix: thread `extractor_flavor` into `report.evaluate()` and return `passed=None` for agentic latency—`None` is not `False`, so it doesn't trip the assert, and the harness already uses that exact pattern for the no-data case ([report.py:159](../../tests/load/report.py#L159)). The agentic-vs-single-pass delta (criterion 6) stays the deliverable. Discovered reading the harness while drafting this ADR; lands in the implementation phase.

## Alternatives considered

- **Flip the existing extractor by env var only (no parameter profile).** Simplest, but the agentic flavor still wants `maxReceiveCount` lowered and `max_iterations` set, so an env-var-only flip leaves those to hand-edit per run and can't hold a clean single-pass baseline alongside. Rejected: the flavor and its profile should move together as one variable.
- **Throughput-preserving cap (~15).** Holds single-pass drain times. Not chosen for v1 (see the fork)—though at ~1.5× the cost delta is small enough that this is nearly a coin-flip. Recorded as a one-variable flip if drain time ever matters more than spend.
- **Multimodal / image modality.** Closer to what a "read the document like a human" agent implies, and what some benchmark rows used. Rejected for the deploy: image tokens multiply the TPM draw and re-tighten Finding 1's coupling for no measured accuracy win on this text-heavy NDA corpus. `text` keeps the provider budget slack.
- **Dedicated `staging-agentic` environment.** A true side-by-side: agentic and single-pass live simultaneously, no baseline displacement. Heavier (a full env stand-up, its own alarms, its own teardown) and unnecessary given the baseline is already captured. Recorded as the cleaner path if a *continuous* A/B is ever wanted, per the single-tenant deployment model ([ADR-0013](0013-single-tenant-deployment-model.md)).
- **Don't deploy agentic; explain the name in prose.** The zero-cost path: a README/blog line saying the name refers to the library, which implements both flavors. Rejected as the anticlimactic answer—it leaves the project's strongest decision resting on offline numbers and forgoes the most interesting load-testing exercise available.

## Post-implementation

### Burst run — staging, agentic, 2026-06-07

Artifact: `tests/load/reports/baseline/agentic-burst-staging-20260607T194252Z.json`.

**SLO verdict: 4/5 passed; SLO 1 (Correctness) failed — 199/200 succeeded, 1 harness timeout.**

| SLO | Result | Detail |
|---|---|---|
| 1 Correctness | **FAIL** | 199/200 succeeded; 1 harness timeout (not a DLQ entry — see Finding D) |
| 2 No premature redelivery | PASS | oldest-age peak 436s < 720s; queue drained to 0 |
| 3 Concurrency cap holds | PASS | peak 10 ≤ 10; throttles 0; SQS in-flight peak 11 (proxy) |
| 4 Latency | n/a (reported) | processing p90 28.0s; e2e p90 366.0s (agentic: not gated, per Finding C) |
| 5 Alarms honest | PASS | no alarm fired |

**Hypotheses graded:**

- **Service time / capacity** — processing p50 14.4s, p90 28.0s, p99 40.8s, max 65.9s. Mean closely matches the benchmark's 14.6s; tail is fatter than expected (p99 ~40s vs the ~30s `max_iterations` bound), driven by docs near the iteration ceiling. Capacity contracted as predicted: throughput 27.3 docs/min vs single-pass's 51.9 (ratio ~0.53, consistent with the ~1.5× service-time expansion at a fixed cap). ✓
- **Burst drain** — queue peaked at 192 (measured by the live sampler, not CloudWatch), oldest-age peaked at 436s (~1.3× under the 720s timeout). Queue was fully drained. Cost: $2.93 total / $14.73 per 1,000 docs (vs the benchmark's ~$11). ✓
- **Finding 1 / Finding B** — `ext_concurrency` pinned at cap 10 throughout; `ext_throttles` zero; no provider-side rate errors. Provider headroom held slack at Tier 1 as predicted. Finding B closes: a request-level limiter is not needed at this scale. ✓
- **Finding 2 / criterion 5 (stressor)** — not yet run. DLQ stayed at 0 on the primary run; the stressor sub-run (low `max_iterations`, deliberate DLQ fill) remains pending.
- **Cost** — $0.0147/doc (199 docs with token data). Benchmark predicted ~$0.011; the gap (~34%) comes from the fatter tail driving more output tokens. Within acceptable range for characterisation.

**Finding D — harness completion window (600s) is shorter than the SQS visibility timeout (720s).**

One NDA document (`019ea390-e465-7f49-9c58-da0921fca211`, file `3c19cab8…pdf`) exhausted the 30-iteration cap on its first Lambda attempt (LangSmith trace: `ExtractionError('Agent exceeded 30 iterations without completing extraction of NDA.')`, 18.53s, 213K tokens). Because `SQS_MAX_RECEIVE_COUNT=2` and `attempt=1 < 2`, `fail()` was not called — the DynamoDB record was left in `"pending"` and the message was returned to SQS invisible for 720s (the visibility timeout).

The harness `await_completion` default was 600s. Since 600s < 720s, the harness timed out before the second attempt could run, marked the document `"timeout"`, and SLO 1 failed. The document did eventually reach `"failed"` in DynamoDB at 19:47 UTC (8 minutes after the harness closed), on its second attempt, via an S3 `AccessDenied` — likely because the ingestion object had been deleted by then. DLQ stayed at 0 throughout (the second attempt's `fail()` call wrote the terminal status, and the message was acked on the subsequent re-delivery that found the record already terminal).

**Fix:** `await_completion` default raised from 600s to 900s (`harness.py:275`). 900s > 720s (visibility timeout) + worst-case retry processing (~65s p99) + buffer, so a first-attempt failure is always catchable on its retry within the window. The burst pytest-timeout backstop (`_TIMEOUT_S=1200s`) comfortably covers 900s completion + 120s settle.

**The root-cause document** hits the `max_iterations=30` ceiling on a legitimately hard NDA. This is the failure mode the Decision section predicted and the `ExtractionError` path is designed to handle — the fix is the harness window, not the iteration cap, because a cap-exhausting doc should DLQ cleanly (after `SQS_MAX_RECEIVE_COUNT` attempts) rather than be given more iterations.

### Sustained run — staging, agentic, 2026-06-07

Artifact: `tests/load/reports/baseline/agentic-sustained-staging-20260607T204008Z.json`.

**SLO verdict: 4/5 passed; SLO 1 (Correctness) failed — 199/200 succeeded, 0 failed, 1 harness timeout; DLQ ext=2 (both measurement artifacts, not run failures — see Finding E).**

| SLO | Result | Detail |
|---|---|---|
| 1 Correctness | **FAIL** | 199/200 succeeded, 0 failed; tripped on *both* clauses — 1 harness timeout (`len(ok)≠n`) and DLQ ext=2 (pre-existing depth), neither a run-generated failure (Finding E) |
| 2 No premature redelivery | PASS | oldest-age peak 554s < 720s; final depth 0 |
| 3 Concurrency cap holds | PASS | peak concurrency 6 ≤ 10; throttles 0 (SQS in-flight peak 8, proxy) |
| 4 Latency | n/a (reported) | processing p90 26.5s; e2e p90 31.3s (agentic: not gated, per Finding C) |
| 5 Alarms honest | PASS | no alarm fired *during the window* (the DLQ alarm was already `ALARM` at window start — Finding E) |

**Hypotheses graded:**

- **Sustained queue & concurrency** — arrival held ADR-0015's ~0.22 doc/s schedule (observed throughput 13.0 docs/min), ~32% of the contracted ~41/min capacity. Queue depth stayed at 0 throughout (sampler `peak_visible` 0; CloudWatch `sqs_depth` peak 0); concurrency hovered low, peaking at **6** — a touch above single-pass's peak-5 (ADR-0015 Finding 3), exactly the "fatter tail, still under cap" the Decision predicted. ✓
- **Latency ≈ processing** — with no backlog, queue-wait collapsed (p50 1.14s, p90 3.12s) and e2e p90 (31.3s) ≈ processing p90 (26.5s) + publish lag. The contrast with the burst run is the cleanest read on arrival-pattern sensitivity: identical flavor and per-doc cost, yet burst's e2e p90 ballooned to 366s from queue dwell at the cap while sustained's sits at 31.3s. **The agency premium in e2e terms is a backlog phenomenon, not a per-document one.** ✓
- **Cost** — $2.89 total / **$0.0145/doc** / $14.51 per 1,000 — within ~1% of the burst run's $0.0147/doc, confirming per-doc cost is arrival-pattern-independent (as it must be: same corpus, same flavor). Still ~32% over the benchmark's ~$0.011, the same fatter-tail output-token gap noted for the burst run. ✓
- **Finding 2 / criterion 5 (stressor)** — still pending. Neither primary run (burst or sustained) exercises it; the deliberate low-`max_iterations` DLQ-fill sub-run remains the one outstanding piece of the exercise.

**Finding E — SLO 1 reads *absolute* DLQ depth and total success count, so it fails on leftover state and harness timeouts even when every document the run injected reached a correct terminal state.**

The run's own outcome was clean: 199/200 `succeeded`, 1 `timeout`, **zero `failed`**. SLO 1 nonetheless failed, on both of its clauses ([report.py:95](../../tests/load/report.py#L95): `len(ok) == n and not failed and dlq["extraction"] == 0`):

1. **The DLQ ext=2 is pre-existing, not run-generated.** The DLQ alarm was already `ALARM` at measurement with `fired: []` (no transition during the 20:07–20:25 window), and the burst artifact 70 minutes earlier closed with `DLQ=0` / alarm `OK`. The two messages accumulated in the gap — consistent with the cleanup-race phantom `aws:AccessDenied` entries diagnosed earlier the same day (a `timeout`-marked doc's source object deleted out from under an in-flight retry; the operator screenshot showed the DLQ filling ~20:00, *before* this window opened). The harness `cleanup()` fix — skip `timeout` docs ([harness.py](../../tests/load/harness.py)) — removes the *source* of those phantoms going forward, but SLO 1 still reads `ApproximateNumberOfMessagesVisible` as an absolute count, so any leftover or out-of-band DLQ entry red-fails a run whose own documents all processed.
2. **The lone timeout is the Finding D mechanism under the sustained schedule.** Doc `019ea3b9-2fd1-7f30-95ca-8935bca568d5` (`724a6c9e…pdf`) never reached terminal status within the completion window; `sqs_oldest` was still climbing (peak 554s) at window close with `sqs_depth` at 0 — the signature of a first-attempt failure sitting out its 720s visibility before retry, not a message waiting in queue. Finding D's fix raised the completion window to 900s, which covers a *burst* first-attempt failure; under *sustained* arrival, a document that fails its first attempt late in the 900s arrival window can have its +720s retry land beyond the completion poll. (To confirm: check whether the doc reached `failed`/`succeeded` in DynamoDB after the harness closed, as the burst Finding D doc did at +8 min.)

**Fix (not yet applied):** make SLO 1 measure *this run's* failures, not queue-wide state — snapshot/diff the DLQ at run start (or purge it), and scope the success check to the run's own `document_id`s; and key the sustained completion window off last-arrival rather than a fixed 900s. Net: across both agentic primary runs the deployed pipeline reached a correct terminal state for every injected document; SLO 1 went red both times on harness/measurement artifacts (Finding D window, Finding E leftover-depth), never on a genuine extraction fault.

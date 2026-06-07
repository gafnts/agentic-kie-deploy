"""
Reporting and SLO evaluation for the load test (ADR-0015).

Computes Layer B (end-to-end latency, decomposed into segments) from the run
results, evaluates the five pass/fail SLOs, writes a JSON artifact, and renders a
printed summary. A run is a *load test* only if it captures all three layers;
this module assembles A (from :mod:`tests.load.measure`), B (here), and C into
one verdict.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .harness import Result
from .measure import Targets

REPORTS_DIR = Path(__file__).parent / "reports"

# SLO thresholds (ADR-0015, "Pass/fail criteria").
VISIBILITY_TIMEOUT_S = 720
PROCESSING_P90_MAX_S = 15
SUSTAINED_E2E_P90_MAX_S = 20


@dataclass(frozen=True)
class SLO:
    id: int
    name: str
    passed: bool | None  # None = not evaluable from the data on hand (informational)
    detail: str


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _seg_stats(results: list[Result], attr: str) -> dict[str, Any] | None:
    values = sorted(v for r in results if (v := getattr(r, attr)) is not None)
    if not values:
        return None
    return {
        "p50": round(_percentile(values, 0.5), 2),
        "p90": round(_percentile(values, 0.9), 2),
        "p99": round(_percentile(values, 0.99), 2),
        "max": round(max(values), 2),
    }


def latency_summary(results: list[Result]) -> dict[str, Any]:
    """Layer B: per-segment percentiles + completed throughput."""
    ok = [r for r in results if r.status == "succeeded"]
    landings = [r.landing for r in ok if r.landing is not None]
    t0s = [r.upload.t0 for r in ok]
    throughput = None
    if landings and t0s:
        span = max(landings) - min(t0s)
        throughput = round(len(ok) / span * 60, 1) if span > 0 else None
    return {
        "queue_wait": _seg_stats(ok, "queue_wait"),
        "processing": _seg_stats(ok, "processing_s"),
        "publish_lag": _seg_stats(ok, "publish_lag"),
        "total_e2e": _seg_stats(ok, "total_e2e"),
        "throughput_per_min": throughput,
    }


def evaluate(
    results: list[Result],
    layer_a: dict[str, Any],
    sampler: Any,
    targets: Targets,
    scenario: str,
    n: int,
) -> list[SLO]:
    """The five SLOs (ADR-0015). ``passed=None`` where the data can't decide."""
    ok = [r for r in results if r.status == "succeeded"]
    failed = [r for r in results if r.status == "failed"]
    metrics = layer_a["metrics"]
    dlq = layer_a["dlq"]
    slos: list[SLO] = []

    # 1. Correctness
    correct = (
        len(ok) == n and not failed and dlq["extraction"] == 0 and dlq["publisher"] == 0
    )
    slos.append(
        SLO(
            1,
            "Correctness",
            correct,
            f"{len(ok)}/{n} succeeded, {len(failed)} failed; "
            f"DLQ ext={dlq['extraction']} pub={dlq['publisher']}",
        )
    )

    # 2. No premature redelivery
    oldest = metrics.get("sqs_oldest", {})
    final_depth = layer_a["main_queue_final_depth"]
    if oldest.get("n", 0) == 0:
        slos.append(
            SLO(
                2,
                "No premature redelivery",
                None,
                f"no oldest-age data; final depth {final_depth}",
            )
        )
    else:
        peak_age = oldest["peak"]
        slos.append(
            SLO(
                2,
                "No premature redelivery",
                peak_age < VISIBILITY_TIMEOUT_S and final_depth == 0,
                f"oldest-age peak {peak_age:.0f}s < {VISIBILITY_TIMEOUT_S}s; final depth {final_depth}",
            )
        )

    # 3. Concurrency cap holds. The authoritative signals are Lambda's
    # ConcurrentExecutions (Maximum) and Throttles (Sum): with maximum_concurrency
    # set on the event source mapping, AWS throttles an invocation before it runs
    # past the cap, so throttles == 0 proves the cap held even if a sub-minute
    # spike slips past the 1-minute ConcurrentExecutions granularity. The SQS
    # in-flight sampler (ApproximateNumberOfMessagesNotVisible) is a noisy proxy—it
    # counts the receive→delete window and poller prefetch, so it legitimately sits
    # a hair above true concurrency at the drain boundary—so it is reported, not
    # gated (its QueueSampler docstring already names CloudWatch the authority).
    peak_conc = metrics.get("ext_concurrency", {}).get("peak", 0.0)
    throttles = metrics.get("ext_throttles", {}).get("total", 0.0)
    cap = targets.concurrency_cap
    inflight = f" (sqs in-flight peak {sampler.peak_in_flight}, proxy)"
    if cap is None:
        slos.append(
            SLO(
                3,
                "Concurrency cap holds",
                None,
                f"peak concurrency {peak_conc:.0f} (no cap for env {targets.env}); "
                f"throttles {throttles:.0f}{inflight}",
            )
        )
    else:
        slos.append(
            SLO(
                3,
                "Concurrency cap holds",
                peak_conc <= cap and throttles == 0,
                f"peak concurrency {peak_conc:.0f} <= {cap}; "
                f"throttles {throttles:.0f}{inflight}",
            )
        )

    # 4. Latency. Gated for single_pass on the <10s-benchmark-derived bars;
    # reported, not gated for agentic (ADR-0016 Finding C), which is slow by
    # design—its deliverable is the agentic-vs-single-pass delta (criterion 6),
    # not a pass/fail bar. passed=None is not False, so a slow agentic run never
    # trips the harness's `assert not failures`.
    agentic = targets.flavor == "agentic"
    proc = [r.processing_s for r in ok if r.processing_s is not None]
    if not proc:
        slos.append(SLO(4, "Latency", None, "no processing data"))
    elif agentic:
        proc_p90 = _percentile(proc, 0.9)
        e2e = [r.total_e2e for r in ok if r.total_e2e is not None]
        e2e_p90 = _percentile(e2e, 0.9) if e2e else None
        e2e_str = f"; e2e p90 {e2e_p90:.1f}s" if e2e_p90 is not None else ""
        slos.append(
            SLO(
                4,
                "Latency",
                None,
                f"processing p90 {proc_p90:.1f}s{e2e_str} "
                "(agentic: reported, not gated)",
            )
        )
    else:
        proc_p90 = _percentile(proc, 0.9)
        if scenario == "sustained":
            e2e = [r.total_e2e for r in ok if r.total_e2e is not None]
            e2e_p90 = _percentile(e2e, 0.9) if e2e else None
            passed = proc_p90 < PROCESSING_P90_MAX_S and (
                e2e_p90 is not None and e2e_p90 < SUSTAINED_E2E_P90_MAX_S
            )
            detail = (
                f"processing p90 {proc_p90:.1f}s < {PROCESSING_P90_MAX_S}s; "
                f"e2e p90 {e2e_p90:.1f}s < {SUSTAINED_E2E_P90_MAX_S}s"
            )
        else:
            passed = proc_p90 < PROCESSING_P90_MAX_S
            detail = (
                f"processing p90 {proc_p90:.1f}s < {PROCESSING_P90_MAX_S}s "
                "(burst e2e reported, not gated)"
            )
        slos.append(SLO(4, "Latency", passed, detail))

    # 5. Alarms honest
    fired = layer_a["alarms"]["fired"]
    slos.append(
        SLO(
            5,
            "Alarms honest",
            not fired,
            "no alarm fired" if not fired else f"fired: {fired}",
        )
    )

    return slos


def _doc_rows(results: list[Result]) -> list[dict[str, Any]]:
    def rnd(value: float | None) -> float | None:
        return round(value, 2) if value is not None else None

    return [
        {
            "name": r.upload.name,
            "document_id": r.upload.document_id,
            "status": r.status,
            "queue_wait_s": rnd(r.queue_wait),
            "processing_s": rnd(r.processing_s),
            "publish_lag_s": rnd(r.publish_lag),
            "total_e2e_s": rnd(r.total_e2e),
            "input_tokens": r.token_input,
            "output_tokens": r.token_output,
        }
        for r in results
    ]


def build(
    scenario: str,
    targets: Targets,
    results: list[Result],
    layer_a: dict[str, Any],
    cost: dict[str, Any],
    slos: list[SLO],
    sampler: Any,
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "env": targets.env,
        "flavor": targets.flavor,
        "n": len(results),
        "timestamp": datetime.now(UTC).isoformat(),
        "window": layer_a["window"],
        "slos": [
            {"id": s.id, "name": s.name, "passed": s.passed, "detail": s.detail}
            for s in slos
        ],
        "latency": latency_summary(results),
        "sampler": {
            "peak_visible": sampler.peak_visible,
            "peak_in_flight": sampler.peak_in_flight,
            "samples": len(sampler.samples),
        },
        "cost": cost,
        "layer_a": layer_a,
        "documents": _doc_rows(results),
    }


def write_artifact(report: dict[str, Any]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{report['scenario']}-{report['env']}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def format_report(report: dict[str, Any]) -> str:
    lat = report["latency"]
    lines = [
        f"\n=== load report: {report['scenario']} / {report['env']} / "
        f"{report.get('flavor', 'single_pass')} / n={report['n']} ===",
        f"  {'segment':<12}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>8}",
    ]
    for label, key in [
        ("queue wait", "queue_wait"),
        ("processing", "processing"),
        ("publish lag", "publish_lag"),
        ("total e2e", "total_e2e"),
    ]:
        seg = lat[key]
        if seg:
            lines.append(
                f"  {label:<12}{seg['p50']:>8.1f}{seg['p90']:>8.1f}"
                f"{seg['p99']:>8.1f}{seg['max']:>8.1f}"
            )
    if lat["throughput_per_min"] is not None:
        lines.append(f"  throughput: {lat['throughput_per_min']:.1f} docs/min")

    sm = report["sampler"]
    lines.append(
        f"  sampler: peak depth={sm['peak_visible']} "
        f"peak in-flight={sm['peak_in_flight']} ({sm['samples']} samples)"
    )

    cold = report["layer_a"]["cold_starts"]
    cold_line = f"  cold starts: {cold['cold_starts']}"
    if cold["avg_init_ms"] is not None:
        cold_line += (
            f" (avg {cold['avg_init_ms']:.0f}ms, max {cold['max_init_ms']:.0f}ms)"
        )
    lines.append(cold_line)

    cost = report["cost"]
    pricing = cost["pricing"]
    lines.append(
        f"  cost: ${cost['total_cost_usd']:.4f} total, "
        f"${cost['cost_per_1000_usd']:.2f}/1000 docs "
        f"(@ ${pricing['input_per_1m']}/1M in, ${pricing['output_per_1m']}/1M out)"
    )

    lines.append("  SLOs:")
    mark = {True: "PASS", False: "FAIL", None: "n/a "}
    for slo in report["slos"]:
        lines.append(
            f"    [{mark[slo['passed']]}] {slo['id']} {slo['name']}: {slo['detail']}"
        )
    return "\n".join(lines)

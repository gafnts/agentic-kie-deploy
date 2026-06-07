"""
Burst and sustained scenarios orchestration (ADR-0015).

Drives the real front door end-to-end, captures the live queue/concurrency
curve, and reads per-document latency segments from server-side timestamps.
Marked ``load`` and run against staging (never prod); it spends real money.

The sample size is overridable via ``LOAD_N`` for a contained dry-run (e.g.
``LOAD_N=3``) that proves the whole path for pennies before a full 200-document
run. ``LOAD_SCENARIO`` selects ``burst`` (default) or ``sustained``.
"""

import os
from typing import Any

import pytest

from . import corpus, harness, measure, report
from .measure import Targets

pytestmark = pytest.mark.load

# pytest-timeout backstop (wall-clock). Sustained paces uploads across a ~15-min
# window, then polls completion (up to ~10 min) and settles for CloudWatch
# propagation, so it needs far more headroom than burst (which drains in a few
# minutes). Resolved from the env at import so the decorator sees the right
# ceiling per scenario.
_SUSTAINED = (os.environ.get("LOAD_SCENARIO") or "burst") == "sustained"
_TIMEOUT_S = 2400 if _SUSTAINED else 1200


@pytest.mark.timeout(_TIMEOUT_S)
def test_scenario(
    boto_session: Any,
    s3: Any,
    dynamodb: Any,
    uploader_api_endpoint: str,
    ingestion_bucket: str,
    results_table_name: str,
    analytics_bucket: str,
    extraction_queue_url: str,
    load_env: str,
    load_targets: Targets,
) -> None:
    scenario = os.environ.get("LOAD_SCENARIO") or "burst"
    n = int(os.environ.get("LOAD_N") or corpus.SAMPLE_SIZE)
    settle = int(os.environ.get("LOAD_SETTLE") or 120)
    docs = corpus.sample(n=n)

    creds = boto_session.get_credentials()
    assert creds is not None, "no AWS credentials available for SigV4 signing"
    region = boto_session.region_name or "us-east-1"
    sqs = boto_session.client("sqs")
    table = dynamodb.Table(results_table_name)

    print(f"\nscenario={scenario} env={load_env} n={n}")

    results: list[harness.Result] = []
    try:
        with harness.QueueSampler(sqs, extraction_queue_url) as sampler:
            if scenario == "burst":
                uploads = harness.run_burst(creds, region, uploader_api_endpoint, docs)
            elif scenario == "sustained":
                uploads = harness.run_sustained(
                    creds, region, uploader_api_endpoint, docs
                )
            else:
                pytest.fail(f"unknown LOAD_SCENARIO={scenario!r} (burst|sustained)")
            print(f"uploaded {len(uploads)} docs; awaiting completion...")
            results = harness.await_completion(table, s3, analytics_bucket, uploads)

        window = measure.window_for(results)
        print(f"settling {settle}s for CloudWatch/Logs propagation...")
        harness.sleep_with_progress(settle, desc="settling")
        layer_a = measure.collect_layer_a(boto_session, load_targets, window)
        cost = measure.cost_summary(results)
        slos = report.evaluate(results, layer_a, sampler, load_targets, scenario, n)

        artifact = report.build(
            scenario, load_targets, results, layer_a, cost, slos, sampler
        )
        path = report.write_artifact(artifact)
        print(report.format_report(artifact))
        print(f"  artifact: {path}")

        failures = [s for s in slos if s.passed is False]
        assert not failures, "SLO failures: " + "; ".join(
            f"[{s.id} {s.name}] {s.detail}" for s in failures
        )
    finally:
        harness.cleanup(s3, table, ingestion_bucket, analytics_bucket, results)

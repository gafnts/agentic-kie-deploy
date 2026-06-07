"""
Post-run measurement for the load test (ADR-0015).

Layer A (pipeline dynamics, CloudWatch): one ``GetMetricData`` pull for the
operational series, one Logs Insights query for extractor cold-start init
durations, one ``DescribeAlarms``+``DescribeAlarmHistory`` read, plus direct
``GetQueueAttributes`` reads of the DLQs and the drained main queue (immediate
and propagation-free, unlike the CloudWatch series).

Layer B (end-to-end latency) is computed from the results in
:mod:`tests.load.report`, since it needs no AWS read.

Layer C (LLM economics): cost derived from the ``token_usage`` already persisted
to each row and carried on the :class:`~tests.load.harness.Result`.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .harness import Result

# gemini-3-flash-preview list price (USD / 1M tokens). Verify against current
# Google pricing before quoting; surfaced in the report so the assumption is
# explicit rather than buried. Recorded 2026-06.
GEMINI_PRICING = {"input_per_1m": 0.50, "output_per_1m": 3.00}


@dataclass(frozen=True)
class Targets:
    """Every resource identifier the measurement layer needs."""

    env: str
    region: str
    queue_url: str
    queue_name: str
    extraction_dlq_name: str
    publisher_dlq_name: str
    extractor_fn: str
    publisher_fn: str
    extractor_log_group: str
    table_name: str
    api_id: str
    analytics_bucket: str
    ingestion_bucket: str
    flavor: str = "single_pass"  # deployed extraction strategy (ADR-0016)

    @property
    def alarm_prefix(self) -> str:
        return f"agentic-kie-deploy-{self.env}-"

    @property
    def concurrency_cap(self) -> int | None:
        """The extractor ``maximum_concurrency`` for this env (ADR-0009)."""
        return {"staging": 10, "prod": 25}.get(self.env)


@dataclass(frozen=True)
class Window:
    """The run's measurement window, in epoch seconds."""

    start: float
    end: float

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start, UTC)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromtimestamp(self.end, UTC)


def window_for(
    results: list[Result], pad_before: float = 60.0, pad_after: float = 120.0
) -> Window:
    """Span the run from first upload to last landing, padded for metric edges."""
    t0s = [r.upload.t0 for r in results]
    ends = [r.landing for r in results if r.landing is not None] or t0s
    return Window(start=min(t0s) - pad_before, end=max(ends) + pad_after)


# LAYER A: CLOUDWATCH METRICS


def _q(
    qid: str, namespace: str, metric: str, dims: dict[str, str], stat: str
) -> dict[str, Any]:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
            },
            "Period": 60,
            "Stat": stat,
        },
    }


def _metric_queries(t: Targets) -> list[dict[str, Any]]:
    main = {"QueueName": t.queue_name}
    ext = {"FunctionName": t.extractor_fn}
    pub = {"FunctionName": t.publisher_fn}
    api = {"ApiId": t.api_id}
    tbl = {"TableName": t.table_name}
    return [
        _q(
            "sqs_depth",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            main,
            "Maximum",
        ),
        _q("sqs_oldest", "AWS/SQS", "ApproximateAgeOfOldestMessage", main, "Maximum"),
        _q("ext_concurrency", "AWS/Lambda", "ConcurrentExecutions", ext, "Maximum"),
        _q("ext_throttles", "AWS/Lambda", "Throttles", ext, "Sum"),
        _q("ext_invocations", "AWS/Lambda", "Invocations", ext, "Sum"),
        _q("ext_errors", "AWS/Lambda", "Errors", ext, "Sum"),
        _q("ext_dur_p50", "AWS/Lambda", "Duration", ext, "p50"),
        _q("ext_dur_p90", "AWS/Lambda", "Duration", ext, "p90"),
        _q("ext_dur_p99", "AWS/Lambda", "Duration", ext, "p99"),
        _q("pub_iterator_age", "AWS/Lambda", "IteratorAge", pub, "Maximum"),
        _q("pub_dur_p90", "AWS/Lambda", "Duration", pub, "p90"),
        _q("pub_errors", "AWS/Lambda", "Errors", pub, "Sum"),
        _q("api_count", "AWS/ApiGateway", "Count", api, "Sum"),
        _q("api_latency_p90", "AWS/ApiGateway", "Latency", api, "p90"),
        _q("api_4xx", "AWS/ApiGateway", "4xx", api, "Sum"),
        _q("api_5xx", "AWS/ApiGateway", "5xx", api, "Sum"),
        _q("ddb_write_throttles", "AWS/DynamoDB", "WriteThrottleEvents", tbl, "Sum"),
    ]


def _pull_metrics(cw: Any, t: Targets, window: Window) -> dict[str, dict[str, Any]]:
    resp = cw.get_metric_data(
        MetricDataQueries=_metric_queries(t),
        StartTime=window.start_dt,
        EndTime=window.end_dt,
        ScanBy="TimestampAscending",
    )
    out: dict[str, dict[str, Any]] = {}
    for series in resp["MetricDataResults"]:
        values = [float(v) for v in series["Values"]]
        out[series["Id"]] = {
            "values": values,
            "peak": max(values, default=0.0),
            "total": sum(values),
            "last": values[-1] if values else 0.0,
            "n": len(values),
        }
    return out


# LAYER A: COLD STARTS (LOGS INSIGHTS)


def _pull_cold_starts(logs: Any, log_group: str, window: Window) -> dict[str, Any]:
    query_id = logs.start_query(
        logGroupName=log_group,
        startTime=int(window.start),
        endTime=int(window.end),
        queryString=(
            "filter ispresent(@initDuration) "
            "| stats count(*) as cold_starts, "
            "avg(@initDuration) as avg_init_ms, "
            "max(@initDuration) as max_init_ms"
        ),
    )["queryId"]

    result: dict[str, Any] = {"status": "Unknown"}
    for _ in range(30):
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)

    rows = result.get("results", [])
    if not rows:
        return {"cold_starts": 0, "avg_init_ms": None, "max_init_ms": None}
    fields = {col["field"]: col["value"] for col in rows[0]}
    return {
        "cold_starts": int(float(fields.get("cold_starts", 0))),
        "avg_init_ms": float(fields["avg_init_ms"])
        if "avg_init_ms" in fields
        else None,
        "max_init_ms": float(fields["max_init_ms"])
        if "max_init_ms" in fields
        else None,
    }


# LAYER A: ALARMS


def _pull_alarms(cw: Any, prefix: str, window: Window) -> dict[str, Any]:
    alarms = cw.describe_alarms(AlarmNamePrefix=prefix)["MetricAlarms"]
    fired: list[str] = []
    for alarm in alarms:
        history = cw.describe_alarm_history(
            AlarmName=alarm["AlarmName"],
            HistoryItemType="StateUpdate",
            StartDate=window.start_dt,
            EndDate=window.end_dt,
            MaxRecords=100,
        )["AlarmHistoryItems"]
        if any("to ALARM" in item.get("HistorySummary", "") for item in history):
            fired.append(alarm["AlarmName"])
    return {
        "count": len(alarms),
        "names": [a["AlarmName"] for a in alarms],
        "fired": fired,
        "current_states": {a["AlarmName"]: a["StateValue"] for a in alarms},
    }


# LAYER A: DIRECT QUEUE DEPTHS (propagation-free)


def _depth(sqs: Any, queue_url: str) -> int:
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"])


def collect_layer_a(session: Any, t: Targets, window: Window) -> dict[str, Any]:
    """Pull the full operational picture for the window."""
    cw = session.client("cloudwatch")
    logs = session.client("logs")
    sqs = session.client("sqs")
    ext_dlq_url = sqs.get_queue_url(QueueName=t.extraction_dlq_name)["QueueUrl"]
    pub_dlq_url = sqs.get_queue_url(QueueName=t.publisher_dlq_name)["QueueUrl"]
    return {
        "window": {
            "start": window.start_dt.isoformat(),
            "end": window.end_dt.isoformat(),
        },
        "metrics": _pull_metrics(cw, t, window),
        "cold_starts": _pull_cold_starts(logs, t.extractor_log_group, window),
        "alarms": _pull_alarms(cw, t.alarm_prefix, window),
        "dlq": {
            "extraction": _depth(sqs, ext_dlq_url),
            "publisher": _depth(sqs, pub_dlq_url),
        },
        "main_queue_final_depth": _depth(sqs, t.queue_url),
    }


# LAYER C: LLM ECONOMICS


def cost_summary(results: list[Result]) -> dict[str, Any]:
    """Cost from persisted token usage (ADR-0015, Layer C)."""
    ok = [r for r in results if r.status == "succeeded"]
    input_tokens = sum(r.token_input or 0 for r in ok)
    output_tokens = sum(r.token_output or 0 for r in ok)
    cost = (
        input_tokens / 1e6 * GEMINI_PRICING["input_per_1m"]
        + output_tokens / 1e6 * GEMINI_PRICING["output_per_1m"]
    )
    n = len(ok)
    return {
        "docs": n,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost_usd": round(cost, 4),
        "cost_per_doc_usd": round(cost / n, 6) if n else 0.0,
        "cost_per_1000_usd": round(cost / n * 1000, 2) if n else 0.0,
        "pricing": GEMINI_PRICING,
    }

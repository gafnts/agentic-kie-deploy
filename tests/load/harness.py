"""
Injection, live sampling, completion tracking, and cleanup for the load test
(ADR-0015).

The harness drives the real front door a caller drives—presign (SigV4) →
presigned PUT → S3 → … → analytics S3—reusing the smoke test's request shape. It
stamps each upload's ``t0`` at PUT completion (object-in-S3) so the queue-wait
segment is pure EventBridge→SQS→poll→dwell, and reads every latency segment from
server-side timestamps, never from the poll's wall-clock (ADR-0015, Layer B).
"""

import concurrent.futures
import contextlib
import json
import random
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from tqdm import tqdm

from .corpus import Document

# PROGRESS


def _countdown(bar: Any, seconds: float) -> None:
    """Sleep ``seconds``, counting it down in ``bar``'s postfix so a paced gap
    reads as a live wait rather than a frozen bar."""
    remaining = seconds
    while remaining > 1e-3:
        bar.set_postfix_str(f"next in {remaining:4.0f}s")
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
    bar.set_postfix_str("")


def sleep_with_progress(seconds: int, desc: str = "settling") -> None:
    """A fixed wait (e.g. metric propagation) rendered as a 1s-tick countdown."""
    for _ in tqdm(range(seconds), desc=desc, unit="s", disable=None):
        time.sleep(1)


# INJECTION


@dataclass(frozen=True)
class Upload:
    """A document pushed through the front door."""

    document_id: str
    name: str  # source filename
    key: str  # ingestion S3 key
    t0: float  # epoch seconds, stamped at PUT completion


def _presign(creds: Any, region: str, api_endpoint: str) -> tuple[str, str]:
    """SigV4 ``POST /uploads`` → (document_id, upload_url)."""
    url = api_endpoint.rstrip("/") + "/uploads"
    signed = AWSRequest(method="POST", url=url, data=b"")
    SigV4Auth(creds, "execute-api", region).add_auth(signed)
    request = urllib.request.Request(
        url, data=b"", method="POST", headers=dict(signed.headers.items())
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        payload = json.loads(resp.read())
    return payload["document_id"], payload["upload_url"]


def upload_one(creds: Any, region: str, api_endpoint: str, doc: Document) -> Upload:
    """Presign, PUT the bytes, and stamp ``t0`` when the object is in S3."""
    document_id, upload_url = _presign(creds, region, api_endpoint)
    key = urllib.parse.unquote(urllib.parse.urlparse(upload_url).path.lstrip("/"))
    request = urllib.request.Request(
        upload_url, data=doc.path.read_bytes(), method="PUT"
    )
    with urllib.request.urlopen(request, timeout=120) as resp:
        if resp.status != 200:
            raise RuntimeError(f"presigned PUT for {doc.name} returned {resp.status}")
    return Upload(document_id=document_id, name=doc.name, key=key, t0=time.time())


def run_burst(
    creds: Any,
    region: str,
    api_endpoint: str,
    docs: list[Document],
    max_workers: int = 32,
) -> list[Upload]:
    """Fan presign+PUT across a thread pool as fast as the client can."""
    workers = min(max_workers, len(docs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(upload_one, creds, region, api_endpoint, d) for d in docs
        ]
        return [
            f.result()
            for f in tqdm(
                concurrent.futures.as_completed(futures),
                total=len(docs),
                desc="upload",
                unit="doc",
                disable=None,
            )
        ]


def run_sustained(
    creds: Any,
    region: str,
    api_endpoint: str,
    docs: list[Document],
    window_s: float = 900.0,
) -> list[Upload]:
    """
    Pace the same uploads evenly across the window with light jitter.

    Each upload is scheduled against an absolute deadline (``start + i*interval``)
    rather than sleeping a full interval *after* it returns, so the upload's own
    cost is absorbed into the gap. The real arrival window stays ``window_s``
    instead of overshooting it by the cumulative upload time (which previously
    stretched a nominal 15-min run to ~18 min and slowed the arrival rate).
    """
    interval = window_s / len(docs)
    rng = random.Random(0)
    uploads: list[Upload] = []
    start = time.time()
    bar = tqdm(docs, desc="upload (paced)", unit="doc", disable=None)
    for i, doc in enumerate(bar):
        uploads.append(upload_one(creds, region, api_endpoint, doc))
        if i < len(docs) - 1:
            jitter = rng.uniform(-interval / 4, interval / 4)
            target = start + (i + 1) * interval + jitter
            _countdown(bar, max(0.0, target - time.time()))
    return uploads


# LIVE SAMPLER


@dataclass(frozen=True)
class QueueSample:
    t: float
    visible: int  # ApproximateNumberOfMessages (queue depth)
    in_flight: int  # ApproximateNumberOfMessagesNotVisible (~concurrency proxy)


class QueueSampler:
    """
    Background poller of SQS depth + in-flight messages.

    CloudWatch's SQS/Lambda series are 1-minute granularity, which smooths past
    the true burst peak on a ~3-4 minute drain; this captures it. In-flight
    (NotVisible) is a live proxy for concurrent executions. The authoritative
    ``ConcurrentExecutions``/``Throttles`` come from the post-run CloudWatch pull
    (Layer A, shot 3).
    """

    def __init__(self, sqs: Any, queue_url: str, interval: float = 3.0):
        self._sqs = sqs
        self._url = queue_url
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.samples: list[QueueSample] = []

    def _poll(self) -> None:
        attrs = self._sqs.get_queue_attributes(
            QueueUrl=self._url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        self.samples.append(
            QueueSample(
                t=time.time(),
                visible=int(attrs["ApproximateNumberOfMessages"]),
                in_flight=int(attrs["ApproximateNumberOfMessagesNotVisible"]),
            )
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                self._poll()
            self._stop.wait(self._interval)

    def __enter__(self) -> "QueueSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval * 2)

    @property
    def peak_visible(self) -> int:
        return max((s.visible for s in self.samples), default=0)

    @property
    def peak_in_flight(self) -> int:
        return max((s.in_flight for s in self.samples), default=0)


# COMPLETION TRACKING


@dataclass
class Result:
    """Per-document outcome with server-side latency segments."""

    upload: Upload
    status: str
    created_at: float | None = None
    completed_at: float | None = None
    processing_ms: int | None = None
    landing: float | None = None
    token_input: int | None = None
    token_output: int | None = None
    error: dict[str, Any] | None = None
    analytics_key: str | None = None

    @property
    def queue_wait(self) -> float | None:
        if self.created_at is None:
            return None
        return self.created_at - self.upload.t0

    @property
    def processing_s(self) -> float | None:
        if self.processing_ms is None:
            return None
        return self.processing_ms / 1000

    @property
    def publish_lag(self) -> float | None:
        if self.landing is None or self.completed_at is None:
            return None
        return self.landing - self.completed_at

    @property
    def total_e2e(self) -> float | None:
        if self.landing is None:
            return None
        return self.landing - self.upload.t0


def _iso_epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _landing_time(s3: Any, bucket: str, key: str) -> Any:
    """The analytics object's ``LastModified``, or None if it hasn't landed."""
    from botocore.exceptions import ClientError

    try:
        return s3.head_object(Bucket=bucket, Key=key)["LastModified"]
    except ClientError:
        return None


def await_completion(
    table: Any,
    s3: Any,
    analytics_bucket: str,
    uploads: list[Upload],
    timeout: float = 900.0,
    interval: float = 5.0,
) -> list[Result]:
    """
    Poll each document to a terminal state, then read its latency segments from
    server-side timestamps (row + analytics object), not the poll wall-clock.

    The default timeout (900s) must exceed the SQS visibility timeout (720s) so
    that a document failing on its first attempt—and thus invisible for 720s before
    retry—can reach a terminal DynamoDB status ("succeeded" or "failed") before the
    harness gives up and marks it "timeout". Any "timeout" result fails SLO 1 even
    when the document eventually resolves correctly on its retry.
    """
    pending = {u.document_id: u for u in uploads}
    done: dict[str, Result] = {}
    deadline = time.time() + timeout
    with tqdm(total=len(uploads), desc="completing", unit="doc", disable=None) as bar:
        while pending and time.time() < deadline:
            for doc_id in list(pending):
                upload = pending[doc_id]
                item = table.get_item(
                    Key={"document_id": doc_id}, ConsistentRead=True
                ).get("Item")
                status = item.get("status") if item else None
                if status not in ("succeeded", "failed"):
                    continue
                if status == "failed":
                    done[doc_id] = Result(
                        upload=upload,
                        status="failed",
                        created_at=_iso_epoch(item["created_at"]),
                        error=item.get("error"),
                    )
                    del pending[doc_id]
                    bar.update(1)
                    continue
                day = datetime.fromisoformat(item["created_at"])
                key = f"extractions/{day:%Y/%m/%d}/{doc_id}.json"
                landed = _landing_time(s3, analytics_bucket, key)
                if landed is None:
                    continue  # row done but result not yet published; keep polling
                usage = item.get("token_usage", {})
                done[doc_id] = Result(
                    upload=upload,
                    status="succeeded",
                    created_at=_iso_epoch(item["created_at"]),
                    completed_at=_iso_epoch(item["completed_at"]),
                    processing_ms=int(item["processing_ms"]),
                    landing=landed.timestamp(),
                    token_input=int(usage.get("input", 0)),
                    token_output=int(usage.get("output", 0)),
                    analytics_key=key,
                )
                del pending[doc_id]
                bar.update(1)
            ok = sum(1 for r in done.values() if r.status == "succeeded")
            bar.set_postfix_str(
                f"ok={ok} failed={len(done) - ok} pending={len(pending)}"
            )
            if pending:
                time.sleep(interval)
    for doc_id, upload in pending.items():
        done[doc_id] = Result(upload=upload, status="timeout")
    return [done[u.document_id] for u in uploads]


def cleanup(
    s3: Any,
    table: Any,
    ingestion_bucket: str,
    analytics_bucket: str,
    results: list[Result],
) -> None:
    """
    Delete every ingestion object, row, and analytics object the run created.

    Skips documents still marked ``timeout``: those never reached a terminal
    DynamoDB status, so the extractor may still be draining them from the SQS
    backlog. Deleting a timeout doc's source object out from under an in-flight
    retry makes ``s3:GetObject`` return 403 — the extractor role has no
    ``s3:ListBucket``, so a missing key surfaces as AccessDenied, not NoSuchKey —
    which lands the message in the DLQ as a phantom failure. Leave their objects
    and rows in place to finish processing; purge them once the backlog drains.
    """
    skipped = [r for r in results if r.status == "timeout"]
    for r in results:
        if r.status == "timeout":
            continue
        with contextlib.suppress(Exception):
            s3.delete_object(Bucket=ingestion_bucket, Key=r.upload.key)
        with contextlib.suppress(Exception):
            table.delete_item(Key={"document_id": r.upload.document_id})
        if r.analytics_key:
            with contextlib.suppress(Exception):
                s3.delete_object(Bucket=analytics_bucket, Key=r.analytics_key)
    if skipped:
        print(
            f"cleanup: left {len(skipped)} timeout doc(s) in place to drain; "
            "purge manually once the backlog clears: "
            + ", ".join(r.upload.document_id for r in skipped)
        )


# REPORTING (shot 2: a compact segment summary; full SLO report lands in shot 3)


def format_segments(results: list[Result]) -> str:
    ok = [r for r in results if r.status == "succeeded"]
    if not ok:
        return "  no successful results"
    lines = [f"  {'segment (s)':<14}{'min':>8}{'median':>9}{'max':>9}"]
    fields = [
        ("queue wait", "queue_wait"),
        ("processing", "processing_s"),
        ("publish lag", "publish_lag"),
        ("total e2e", "total_e2e"),
    ]
    for label, attr in fields:
        vals = sorted(v for r in ok if (v := getattr(r, attr)) is not None)
        if not vals:
            continue
        lo, med, hi = vals[0], vals[len(vals) // 2], vals[-1]
        lines.append(f"  {label:<14}{lo:>8.1f}{med:>9.1f}{hi:>9.1f}")
    return "\n".join(lines)

"""
Results publisher Lambda.

Implements the contract defined in ADR-0012: consumes the results table's
DynamoDB Stream (NEW_IMAGE, per ADR-0007) and fans each terminal row out to an
S3 result object at ``extractions/{yyyy}/{mm}/{dd}/{document_id}.json``. The
object is the same bytes the caller reads (ADR-0011) and Athena queries
(ADR-0012): single-line JSON, ``application/json``.

Per stream record it does four things: skip anything that is not an INSERT or
MODIFY landing on a terminal ``status`` (the event source mapping's filter does
the primary cut; this is defense in depth), project ``NEW_IMAGE`` into the
payload minus ``ttl``, compose the key from ``created_at`` (the claim day, so a
document that crosses midnight between phases stays on one partition), and
``PutObject``.

Idempotency is free: the key is ``{document_id}.json`` and the payload is a pure
function of ``NEW_IMAGE``, so a redelivered record overwrites with identical
bytes. Failures are reported per-record via ``ReportBatchItemFailures`` so a
single poison record does not re-drive the rest of the batch.

Stdlib only beyond ``boto3`` (already in the Lambda runtime) so the zip is a
single file and Terraform packages it directly with ``archive_file``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from decimal import Decimal
from functools import cache
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TERMINAL_STATUSES = ("succeeded", "failed")

_deserializer = TypeDeserializer()


@cache
def _s3_client() -> Any:
    return boto3.client("s3")


def log(outcome: str, **fields: Any) -> None:
    """Emit a single structured JSON log line."""
    logger.info(json.dumps({"handler_outcome": outcome, **fields}))


def _json_default(value: Any) -> Any:
    """Render DynamoDB numbers (Decimal) as ints when integral, else floats."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def project(new_image: dict[str, Any]) -> dict[str, Any]:
    """Deserialize a DynamoDB NEW_IMAGE into the result payload, dropping ``ttl``."""
    item = {key: _deserializer.deserialize(value) for key, value in new_image.items()}
    item.pop("ttl", None)
    return item


def serialize(item: dict[str, Any]) -> str:
    """Serialize the payload as single-line JSON (what Athena's JSON SerDe expects)."""
    return json.dumps(item, default=_json_default, separators=(",", ":"))


def compose_key(created_at: str, document_id: str) -> str:
    """Compose the analytics object key from the claim day (ADR-0011 / ADR-0012)."""
    day = datetime.datetime.fromisoformat(created_at)
    return f"extractions/{day:%Y/%m/%d}/{document_id}.json"


def put_result(key: str, item: dict[str, Any]) -> None:
    """Write the result payload to the analytics bucket as application/json."""
    _s3_client().put_object(
        Bucket=os.environ["ANALYTICS_BUCKET_NAME"],
        Key=key,
        Body=serialize(item).encode("utf-8"),
        ContentType="application/json",
    )


def process_record(record: dict[str, Any]) -> str | None:
    """
    Publish one stream record. Returns the sequence number to fail, or None to ack.
    """
    ddb: dict[str, Any] = record.get("dynamodb", {})
    sequence_number: str | None = ddb.get("SequenceNumber")
    stream_record_id = record.get("eventID")
    event_name = record.get("eventName")
    new_image = ddb.get("NewImage")

    if event_name not in ("INSERT", "MODIFY") or not new_image:
        log(
            "skipped_non_terminal",
            stream_record_id=stream_record_id,
            event_name=event_name,
        )
        return None

    item = project(new_image)
    document_id = item.get("document_id")
    status = item.get("status")

    if status not in _TERMINAL_STATUSES:
        log(
            "skipped_non_terminal",
            stream_record_id=stream_record_id,
            document_id=document_id,
            status=status,
        )
        return None

    try:
        key = compose_key(item["created_at"], item["document_id"])
        put_result(key, item)
    except Exception as exc:
        logger.exception("publish failed for document_id=%s", document_id)
        log(
            "failed",
            stream_record_id=stream_record_id,
            document_id=document_id,
            error=str(exc)[:200],
        )
        return sequence_number

    log(
        "published",
        stream_record_id=stream_record_id,
        document_id=document_id,
        status=status,
        key=key,
    )
    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point: publish each stream record and report batch item failures."""
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        sequence_number = process_record(record)
        if sequence_number is not None:
            failures.append({"itemIdentifier": sequence_number})
    return {"batchItemFailures": failures}

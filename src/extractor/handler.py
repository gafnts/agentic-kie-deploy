"""
Extractor Lambda handler.

Stub implementation of the contract defined in ADR-0009: parses ``document_id``
from the S3 object key, runs the two-phase conditional-write idempotency state
machine against DynamoDB, and flushes LangSmith on the way out. The actual
``agentic-kie`` invocation is replaced with a placeholder result so the pipeline
is deployable end-to-end before the library is wired in.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


LLM_PROVIDER_SECRET_ARN = os.environ["LLM_PROVIDER_SECRET_ARN"]
LANGSMITH_SECRET_ARN = os.environ["LANGSMITH_SECRET_ARN"]
LANGSMITH_PROJECT = os.environ["LANGSMITH_PROJECT"]
RESULTS_TABLE_NAME = os.environ["RESULTS_TABLE_NAME"]

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(RESULTS_TABLE_NAME)


def fetch_secret(arn: str) -> Any:
    return secrets_client.get_secret_value(SecretId=arn)["SecretString"]


# Fetched once per execution environment; reused across warm invocations
# (ADR-0009: "Lambda fetches both secrets once at cold start...").
LLM_API_KEY = fetch_secret(LLM_PROVIDER_SECRET_ARN)

# LangSmith SDK reads these from the environment; populating them after
# the GetSecretValue call keeps the key off the Lambda configuration.
os.environ["LANGSMITH_API_KEY"] = fetch_secret(LANGSMITH_SECRET_ARN)
os.environ["LANGSMITH_TRACING"] = "true"

from langsmith import Client as LangSmithClient  # noqa: E402
from langsmith import traceable  # noqa: E402

ls_client = LangSmithClient()


# uploads/{yyyy}/{mm}/{dd}/{document_id}  (ADR-0006).
DOC_ID_RE = re.compile(
    r"^uploads/\d{4}/\d{2}/\d{2}/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def parse_document_id(key: str) -> str | None:
    match = DOC_ID_RE.match(key)
    return match.group(1) if match else None


def iso_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def log(outcome: str, **fields: Any) -> None:
    logger.info(json.dumps({"handler_outcome": outcome, **fields}))


@traceable(name="extract_document", project_name=LANGSMITH_PROJECT)
def extract(bucket: str, key: str, document_id: str) -> dict[str, Any]:
    """Stub for the agentic-kie call.

    A real implementation would download ``s3://bucket/key``, run the
    extractor, and return the structured answer plus metadata. The shape
    below matches the optional attributes in the table schema (ADR-0007)
    so the conditional UPDATE in :func:`complete` works as-is.
    """
    return {
        "extracted_fields": {},
        "model_version": "stub-0",
        "token_usage": {"input": 0, "output": 0},
        "processing_ms": 0,
    }


def claim(document_id: str) -> None:
    table.put_item(
        Item={
            "document_id": document_id,
            "status": "pending",
            "created_at": iso_now(),
        },
        ConditionExpression="attribute_not_exists(document_id)",
    )


def read_status(document_id: str) -> str | None:
    resp = table.get_item(Key={"document_id": document_id}, ConsistentRead=True)
    item = resp.get("Item")
    return item.get("status") if item else None


def complete(document_id: str, result: dict[str, Any]) -> None:
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression=(
            "SET #s = :new, completed_at = :now, "
            "extracted_fields = :ef, model_version = :mv, "
            "token_usage = :tu, processing_ms = :pm"
        ),
        ConditionExpression="#s = :pending",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":new": "succeeded",
            ":pending": "pending",
            ":now": iso_now(),
            ":ef": result["extracted_fields"],
            ":mv": result["model_version"],
            ":tu": result["token_usage"],
            ":pm": result["processing_ms"],
        },
    )


def fail(document_id: str, error_code: str, error_message: str) -> None:
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET #s = :new, completed_at = :now, #e = :err",
        ConditionExpression="#s = :pending",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":new": "failed",
            ":pending": "pending",
            ":now": iso_now(),
            ":err": {"code": error_code, "message": error_message[:512]},
        },
    )


def process_record(record: dict[str, Any]) -> str | None:
    """Process one SQS record. Returns the messageId to fail, or None to ack."""
    message_id = record.get("messageId")
    attempt = int(record.get("attributes", {}).get("ApproximateReceiveCount", "1"))

    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError:
        log(
            "failed", reason="invalid_json_body", message_id=message_id, attempt=attempt
        )
        return None  # poison-pill: ack

    detail = body.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    key = detail.get("object", {}).get("key")

    if not bucket or not key:
        log(
            "failed",
            reason="missing_bucket_or_key",
            message_id=message_id,
            attempt=attempt,
        )
        return None  # poison-pill: ack

    document_id = parse_document_id(key)
    if document_id is None:
        log(
            "failed",
            reason="unparseable_key",
            message_id=message_id,
            attempt=attempt,
            key=key,
        )
        return None  # poison-pill: ack

    try:
        claim(document_id)
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            != "ConditionalCheckFailedException"
        ):
            raise
        status = read_status(document_id)
        if status in ("succeeded", "failed"):
            log(
                "redelivery_noop",
                document_id=document_id,
                message_id=message_id,
                attempt=attempt,
                status=status,
            )
            return None  # already terminal: ack
        # status == "pending": a sibling delivery is still in-flight.
        # Let the visibility timeout act as the synchronization window.
        log(
            "failed",
            reason="claim_pending",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
        )
        return message_id

    try:
        result = extract(bucket, key, document_id)
        complete(document_id, result)
        log(
            "succeeded",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
        )
        return None
    except Exception as exc:
        logger.exception("extraction failed for %s", document_id)
        with contextlib.suppress(ClientError):
            fail(document_id, type(exc).__name__, str(exc))
        log(
            "failed",
            reason="extract_error",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
            error=str(exc)[:200],
        )
        return message_id


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    try:
        for record in event.get("Records", []):
            message_id = process_record(record)
            if message_id is not None:
                failures.append({"itemIdentifier": message_id})
        return {"batchItemFailures": failures}
    finally:
        try:
            ls_client.flush()
        except Exception:
            logger.exception("langsmith flush failed")

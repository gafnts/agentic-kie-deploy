"""
Extractor Lambda handler.

Implements the contract defined in ADR-0009: parses ``document_id`` from the S3
object key, runs the two-phase conditional-write idempotency state machine
against DynamoDB, invokes ``agentic-kie`` to extract structured NDA fields, and
flushes LangSmith on the way out. Module-level work is deferred via cached
getters so the module is importable without AWS credentials or env config.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
from functools import cache
from typing import Any, cast

import boto3
from agentic_kie import AgenticExtractor, Extractor, PDFLoader, SinglePassExtractor
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import Client as LangSmithClient
from langsmith import traceable
from langsmith.run_trees import get_cached_client
from schema import NDA

logger = Logger()


# uploads/{yyyy}/{mm}/{dd}/{document_id}
DOC_ID_RE = re.compile(
    r"^uploads/\d{4}/\d{2}/\d{2}/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


class ExtractionError(Exception):
    """Sanitized error surfaced through ``@traceable``. CloudWatch retains the original cause."""


def _redact_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Strip bucket/key from the LangSmith trace input; keep ``document_id`` as the correlation key."""
    return {"document_id": inputs.get("document_id")}


@cache
def _secrets_client() -> Any:
    return boto3.client("secretsmanager")


@cache
def _s3_client() -> Any:
    return boto3.client("s3")


@cache
def _table() -> Any:
    return boto3.resource("dynamodb").Table(os.environ["RESULTS_TABLE_NAME"])


def _fetch_secret(arn: str) -> str:
    """Retrieve a secret string from AWS Secrets Manager by ARN."""
    return cast(str, _secrets_client().get_secret_value(SecretId=arn)["SecretString"])


@cache
def _llm_api_key() -> str:
    """Fetch the LLM provider API key once; kept out of os.environ to avoid printenv exposure."""
    return _fetch_secret(os.environ["LLM_PROVIDER_SECRET_ARN"])


@cache
def _bootstrap_secrets() -> None:
    """Hydrate LangSmith SDK env vars from Secrets Manager. Runs once per execution environment."""
    os.environ["LANGSMITH_API_KEY"] = _fetch_secret(os.environ["LANGSMITH_SECRET_ARN"])
    os.environ["LANGSMITH_TRACING"] = "true"


@cache
def _extractor() -> Extractor[NDA]:
    """
    Build the key information extractor for the deployed flavor (ADR-0016).

    ``EXTRACTOR_FLAVOR`` selects the strategy: ``single_pass`` (default) issues
    one structured LLM call; ``agentic`` runs a ReAct loop over the document,
    capped at ``EXTRACTOR_MAX_ITERATIONS`` LangGraph supersteps. Both satisfy the
    ``Extractor`` protocol and share the identical ``(model, schema)`` interface,
    so the handler's broad ``except`` routes either one's failure—including the
    agentic non-termination ``ExtractionError``—through the same redrive path.
    """
    _bootstrap_secrets()
    model = ChatGoogleGenerativeAI(
        model=os.environ["LLM_MODEL"], google_api_key=_llm_api_key()
    )
    if os.environ.get("EXTRACTOR_FLAVOR", "single_pass") == "agentic":
        return AgenticExtractor(
            model=model,
            schema=NDA,
            modality="text",
            max_iterations=int(os.environ.get("EXTRACTOR_MAX_ITERATIONS", "30")),
        )
    return SinglePassExtractor(model=model, schema=NDA)


@cache
def _ls_client() -> LangSmithClient:
    """Return the singleton LangSmith client used by ``@traceable`` so flushing drains its queue."""
    _bootstrap_secrets()
    return get_cached_client()


def parse_document_id(key: str) -> str | None:
    """Extract the document UUID from an S3 object key, or None if the key doesn't match."""
    match = DOC_ID_RE.match(key)
    return match.group(1) if match else None


def iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.datetime.now(datetime.UTC).isoformat()


def log(outcome: str, **fields: Any) -> None:
    """Emit a structured log entry with the given outcome and extra fields."""
    logger.info({"handler_outcome": outcome, **fields})


@traceable(name="extract_document", process_inputs=_redact_inputs)
def extract(bucket: str, key: str, document_id: str) -> dict[str, Any]:
    """
    Download ``s3://bucket/key``, run the deployed flavor's NDA extraction, and return structured results.

    Return shape matches the optional attributes in the table schema (ADR-0007)
    so the conditional UPDATE in :func:`complete` works as-is.

    ``ClientError`` from boto3 embeds account ID and resource ARNs in its message; it is
    translated to ``ExtractionError`` so those identifiers do not leave the AWS account
    via the LangSmith trace. The original cause is logged to CloudWatch.
    """
    try:
        data = _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        logger.exception(
            "aws error during extraction", extra={"document_id": document_id}
        )
        raise ExtractionError(f"aws:{code}") from None

    document = PDFLoader().load_bytes(data, name=key)

    start = time.perf_counter()
    result = _extractor().extract(document)
    processing_ms = round((time.perf_counter() - start) * 1000)

    return {
        "extracted_fields": result.value.model_dump(),
        "model_version": os.environ["LLM_MODEL"],
        "token_usage": {
            "input": result.usage["input_tokens"],
            "output": result.usage["output_tokens"],
        },
        "processing_ms": processing_ms,
    }


def claim(document_id: str) -> None:
    """
    Conditionally insert a pending record for document_id; raises if it already exists.
    """
    _table().put_item(
        Item={
            "document_id": document_id,
            "status": "pending",
            "created_at": iso_now(),
        },
        ConditionExpression="attribute_not_exists(document_id)",
    )


def read_status(document_id: str) -> str | None:
    """
    Return the current DynamoDB status for document_id, or None if not found.
    """
    resp = _table().get_item(Key={"document_id": document_id}, ConsistentRead=True)
    item = resp.get("Item")
    return item.get("status") if item else None


def complete(document_id: str, result: dict[str, Any]) -> None:
    """
    Conditionally transition document_id from pending to succeeded and write extraction results.
    """
    _table().update_item(
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
    """
    Conditionally transition document_id from pending to failed and record the error.
    """
    _table().update_item(
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
    """
    Process one SQS record. Returns the messageId to fail, or None to ack.
    """
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
        log(
            "failed",
            reason="claim_pending",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
        )
        return message_id

    logger.info(
        {"step": "claimed", "document_id": document_id, "message_id": message_id}
    )

    try:
        logger.info(
            {
                "step": "extracting",
                "document_id": document_id,
                "bucket": bucket,
                "key": key,
            }
        )
        result = extract(bucket, key, document_id)
        logger.info(
            {
                "step": "extracted",
                "document_id": document_id,
                "processing_ms": result["processing_ms"],
                "token_usage": result["token_usage"],
            }
        )
        complete(document_id, result)
        logger.info({"step": "completed", "document_id": document_id})
        log(
            "succeeded",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
        )
        return None
    except Exception as exc:
        logger.exception("extraction failed for %s", document_id)
        max_receive_count = int(os.environ.get("SQS_MAX_RECEIVE_COUNT", "3"))
        if attempt >= max_receive_count:
            try:
                fail(document_id, type(exc).__name__, str(exc))
            except ClientError as fail_exc:
                if (
                    fail_exc.response.get("Error", {}).get("Code")
                    != "ConditionalCheckFailedException"
                ):
                    logger.warning(
                        "failed to write terminal status for %s: %s",
                        document_id,
                        fail_exc,
                    )
        log(
            "failed",
            reason="extract_error",
            document_id=document_id,
            message_id=message_id,
            attempt=attempt,
            error=str(exc)[:200],
        )
        return message_id


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """
    Lambda entry point: process each SQS record and return batch item failures.
    """
    _bootstrap_secrets()
    failures: list[dict[str, str]] = []
    try:
        for record in event.get("Records", []):
            message_id = process_record(record)
            if message_id is not None:
                failures.append({"itemIdentifier": message_id})
        return {"batchItemFailures": failures}
    finally:
        try:
            _ls_client().flush()
        except Exception:
            logger.exception("LangSmith flush failed")

"""
Uploader presigner Lambda.

Implements the contract defined in ADR-0010: mints a UUIDv7 document ID,
composes the date-sharded S3 key per ADR-0006, and returns a short-lived
pre-signed PUT URL alongside the ID and its expiry. The function is fronted
by an API Gateway HTTP API with ``AWS_IAM`` authorization, so the caller's
IAM principal (surfaced via the request context) is the authoritative
identity for every upload.

Stdlib only beyond ``boto3`` (already in the Lambda runtime) so the zip is a
single file and Terraform packages it directly with ``archive_file``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import secrets
import time
from functools import cache
from typing import Any, cast

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@cache
def _s3_client() -> Any:
    # SigV4 is required: the Lambda runs with STS credentials (ASIA...),
    # and SigV2 does not bind the session token into the signature.
    return boto3.client("s3", config=Config(signature_version="s3v4"))


def uuid7() -> str:
    """
    Generate a UUIDv7 (RFC 9562): 48-bit Unix-millisecond timestamp,
    12 bits of sub-millisecond randomness, 62 bits of random tail, with
    the version (0b0111) and variant (0b10) bits fixed.

    Inlined here so the zip ships as a single file; ``uuid.uuid7`` is
    not in the stdlib of the Python 3.13 Lambda runtime.
    """
    unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    n = unix_ms << 80
    n |= 0x7 << 76
    n |= rand_a << 64
    n |= 0b10 << 62
    n |= rand_b

    h = f"{n:032x}"
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def compose_key(document_id: str, now: datetime.datetime) -> str:
    """Compose the date-sharded ingestion key per ADR-0006."""
    return f"uploads/{now:%Y/%m/%d}/{document_id}"


def presign(bucket: str, key: str, ttl_seconds: int) -> str:
    """Generate a pre-signed S3 PUT URL valid for ``ttl_seconds``."""
    return str(
        _s3_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
    )


def _client_principal(event: dict[str, Any]) -> str | None:
    """Return the caller's IAM principal ARN from the API Gateway request context."""
    try:
        return cast(str, event["requestContext"]["authorizer"]["iam"]["userArn"])
    except (KeyError, TypeError):
        return None


def _request_id(event: dict[str, Any]) -> str | None:
    try:
        return cast(str, event["requestContext"]["requestId"])
    except (KeyError, TypeError):
        return None


def log(outcome: str, **fields: Any) -> None:
    """Emit a single structured JSON log line."""
    logger.info(json.dumps({"handler_outcome": outcome, **fields}))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Mint a document_id, sign a PUT URL, return the address-before-existence response."""
    bucket = os.environ["INGESTION_BUCKET_NAME"]
    ttl = int(os.environ["URL_TTL_SECONDS"])

    now = datetime.datetime.now(datetime.UTC)
    document_id = uuid7()
    key = compose_key(document_id, now)
    upload_url = presign(bucket, key, ttl)
    expires_at = (now + datetime.timedelta(seconds=ttl)).isoformat()

    log(
        "succeeded",
        document_id=document_id,
        client_principal=_client_principal(event),
        request_id=_request_id(event),
        key=key,
        ttl_seconds=ttl,
    )

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "document_id": document_id,
                "upload_url": upload_url,
                "expires_at": expires_at,
            }
        ),
    }

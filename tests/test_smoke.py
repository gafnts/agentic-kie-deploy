"""
End-to-end smoke tests against deployed infrastructure.

All tests in this module are marked ``integration`` and deselected by default.
Run via ``make smoke`` or ``uv run pytest -m integration --override-ini='addopts='``.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tqdm
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

pytestmark = pytest.mark.integration

_PDF_PATH = Path(__file__).parent / "static/smoke_document.pdf"


class TestExtractorSmoke:
    """
    S3 (with extractor key shape) → Lambda → DynamoDB.
    """

    @pytest.mark.timeout(300)
    def test_pdf_upload_extracts_to_dynamodb(
        self,
        s3: Any,
        dynamodb: Any,
        ingestion_bucket: str,
        results_table_name: str,
    ) -> None:
        doc_id = str(uuid.uuid4())
        today = datetime.now(UTC)
        key = f"uploads/{today:%Y/%m/%d}/{doc_id}"

        s3.put_object(Bucket=ingestion_bucket, Key=key, Body=_PDF_PATH.read_bytes())
        table = dynamodb.Table(results_table_name)

        try:
            deadline = time.time() + 180
            prev = time.time()
            with tqdm.tqdm(
                total=180,
                desc="S3 → Lambda → DynamoDB",
                unit="s",
                bar_format="{l_bar}{bar}| {n:.0f}/{total:.0f}s",
            ) as bar:
                while time.time() < deadline:
                    item = table.get_item(
                        Key={"document_id": doc_id}, ConsistentRead=True
                    ).get("Item")
                    status = item.get("status") if item else None
                    bar.set_postfix_str(status or "pending")
                    if status == "succeeded":
                        assert item["extracted_fields"]["party"], (
                            "extracted_fields.party should be non-empty"
                        )
                        return
                    if status == "failed":
                        pytest.fail(f"extraction failed: {item.get('error', {})}")
                    time.sleep(5)
                    now = time.time()
                    bar.update(now - prev)
                    prev = now
            pytest.fail(f"document {doc_id} did not reach 'succeeded' within 180s")
        finally:
            s3.delete_object(Bucket=ingestion_bucket, Key=key)
            table.delete_item(Key={"document_id": doc_id})


class TestUploaderSmoke:
    """
    HTTP API (SigV4) → presigned PUT → S3 → Lambda → DynamoDB.
    """

    @pytest.mark.timeout(300)
    def test_presigned_upload_extracts_to_dynamodb(
        self,
        boto_session: Any,
        s3: Any,
        dynamodb: Any,
        uploader_api_endpoint: str,
        ingestion_bucket: str,
        results_table_name: str,
    ) -> None:
        creds = boto_session.get_credentials()
        assert creds is not None, "no AWS credentials available for SigV4 signing"
        region = boto_session.region_name or "us-east-1"

        url = uploader_api_endpoint.rstrip("/") + "/uploads"
        signed = AWSRequest(method="POST", url=url, data=b"")
        SigV4Auth(creds, "execute-api", region).add_auth(signed)

        with urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=b"",
                method="POST",
                headers=dict(signed.headers.items()),
            ),
            timeout=30,
        ) as resp:
            payload = json.loads(resp.read())

        document_id = payload["document_id"]
        upload_url = payload["upload_url"]
        key = urllib.parse.unquote(urllib.parse.urlparse(upload_url).path.lstrip("/"))

        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    upload_url, data=_PDF_PATH.read_bytes(), method="PUT"
                ),
                timeout=60,
            ) as resp:
                assert resp.status == 200, f"presigned PUT returned {resp.status}"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            pytest.fail(
                f"presigned PUT failed with {e.code} {e.reason}\n"
                f"url: {upload_url}\n"
                f"body: {body}"
            )

        table = dynamodb.Table(results_table_name)

        try:
            deadline = time.time() + 180
            prev = time.time()
            with tqdm.tqdm(
                total=180,
                desc="API → S3 → Lambda → DynamoDB",
                unit="s",
                bar_format="{l_bar}{bar}| {n:.0f}/{total:.0f}s",
            ) as bar:
                while time.time() < deadline:
                    item = table.get_item(
                        Key={"document_id": document_id}, ConsistentRead=True
                    ).get("Item")
                    status = item.get("status") if item else None
                    bar.set_postfix_str(status or "pending")
                    if status == "succeeded":
                        assert item["extracted_fields"]["party"], (
                            "extracted_fields.party should be non-empty"
                        )
                        return
                    if status == "failed":
                        pytest.fail(f"extraction failed: {item.get('error', {})}")
                    time.sleep(5)
                    now = time.time()
                    bar.update(now - prev)
                    prev = now
            pytest.fail(f"document {document_id} did not reach 'succeeded' within 180s")
        finally:
            s3.delete_object(Bucket=ingestion_bucket, Key=key)
            table.delete_item(Key={"document_id": document_id})

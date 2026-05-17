"""
End-to-end smoke tests against deployed infrastructure.

All tests in this module are marked ``integration`` and deselected by default.
Run via ``make smoke`` or ``uv run pytest -m integration --override-ini='addopts='``.
"""

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tqdm

pytestmark = pytest.mark.integration

_PDF_PATH = Path(__file__).parent / "static/smoke_document.pdf"


class TestQueueSmoke:
    """
    S3 → EventBridge → SQS.
    """

    def test_s3_upload_arrives_in_extraction_queue(
        self,
        s3: Any,
        sqs: Any,
        ingestion_bucket: str,
        extraction_queue_url: str,
    ) -> None:
        run_id = f"smoke-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        key = f"smoke/{run_id}.txt"
        s3.put_object(Bucket=ingestion_bucket, Key=key, Body=run_id.encode())

        try:
            deadline = time.time() + 30
            prev = time.time()
            with tqdm.tqdm(
                total=30,
                desc="S3 → SQS",
                unit="s",
                bar_format="{l_bar}{bar}| {n:.0f}/{total:.0f}s",
            ) as bar:
                while time.time() < deadline:
                    resp = sqs.receive_message(
                        QueueUrl=extraction_queue_url,
                        WaitTimeSeconds=5,
                        MaxNumberOfMessages=10,
                    )
                    for msg in resp.get("Messages", []):
                        if key in msg["Body"]:
                            sqs.delete_message(
                                QueueUrl=extraction_queue_url,
                                ReceiptHandle=msg["ReceiptHandle"],
                            )
                            return
                        sqs.change_message_visibility(
                            QueueUrl=extraction_queue_url,
                            ReceiptHandle=msg["ReceiptHandle"],
                            VisibilityTimeout=0,
                        )
                    now = time.time()
                    bar.update(now - prev)
                    prev = now
            pytest.fail(f"no message referencing {key} arrived within 30s")
        finally:
            s3.delete_object(Bucket=ingestion_bucket, Key=key)


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

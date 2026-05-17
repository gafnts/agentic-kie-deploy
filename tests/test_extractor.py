"""
Tests for the extractor Lambda handler (src/extractor/handler.py).
"""

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import handler
import pytest
from botocore.exceptions import ClientError
from schema import NDA

_VALID_KEY = "uploads/2026/05/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_DOC_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _s3_event_body(*, bucket: str = "b", key: str = _VALID_KEY) -> str:
    return json.dumps({"detail": {"bucket": {"name": bucket}, "object": {"key": key}}})


def _record(
    *, body: str = "{}", message_id: str = "msg-1", receive_count: int = 1
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
        "body": body,
    }


def _ccfe(op: str = "PutItem") -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, op)


class TestParseDocumentId:
    def test_valid_key_returns_uuid(self):
        key = "uploads/2026/05/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert handler.parse_document_id(key) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @pytest.mark.parametrize(
        "key",
        [
            "garbage/key",
            # wrong prefix
            "smoke/2026/05/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            # 2-digit year
            "uploads/26/05/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            # 1-digit month
            "uploads/2026/5/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            # uppercase hex (regex requires lowercase)
            "uploads/2026/05/17/AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
            # trailing suffix (regex anchors $)
            "uploads/2026/05/17/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.pdf",
            # malformed uuid
            "uploads/2026/05/17/not-a-uuid",
            # short first uuid segment
            "uploads/2026/05/17/aaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            # empty
            "",
            # missing uuid
            "uploads/2026/05/17/",
        ],
    )
    def test_malformed_key_returns_none(self, key):
        assert handler.parse_document_id(key) is None


class TestClaim:
    def test_puts_pending_item_with_conditional_check(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        handler.claim("doc-123")

        kwargs = fake.put_item.call_args.kwargs
        assert kwargs["Item"]["document_id"] == "doc-123"
        assert kwargs["Item"]["status"] == "pending"
        assert "created_at" in kwargs["Item"]
        assert kwargs["ConditionExpression"] == "attribute_not_exists(document_id)"


class TestReadStatus:
    def test_returns_status_when_row_exists(self, monkeypatch):
        fake = MagicMock()
        fake.get_item.return_value = {"Item": {"status": "pending"}}
        monkeypatch.setattr(handler, "_table", lambda: fake)

        assert handler.read_status("doc-123") == "pending"
        assert fake.get_item.call_args.kwargs == {
            "Key": {"document_id": "doc-123"},
            "ConsistentRead": True,
        }

    def test_returns_none_when_row_absent(self, monkeypatch):
        fake = MagicMock()
        fake.get_item.return_value = {}
        monkeypatch.setattr(handler, "_table", lambda: fake)

        assert handler.read_status("doc-123") is None


class TestComplete:
    def test_writes_terminal_record_with_pending_guard(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        result = {
            "extracted_fields": {"party": ["Acme"]},
            "model_version": "gemini-1.5-pro",
            "token_usage": {"input": 100, "output": 50},
            "processing_ms": 1234,
        }
        handler.complete("doc-123", result)

        kwargs = fake.update_item.call_args.kwargs
        assert kwargs["Key"] == {"document_id": "doc-123"}
        assert kwargs["ConditionExpression"] == "#s = :pending"
        assert kwargs["ExpressionAttributeNames"] == {"#s": "status"}
        values = kwargs["ExpressionAttributeValues"]
        assert values[":new"] == "succeeded"
        assert values[":pending"] == "pending"
        assert values[":ef"] == result["extracted_fields"]
        assert values[":mv"] == result["model_version"]
        assert values[":tu"] == result["token_usage"]
        assert values[":pm"] == result["processing_ms"]


class TestFail:
    def test_writes_failure_with_pending_guard(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        handler.fail("doc-123", "ValueError", "something broke")

        kwargs = fake.update_item.call_args.kwargs
        assert kwargs["Key"] == {"document_id": "doc-123"}
        assert kwargs["ConditionExpression"] == "#s = :pending"
        assert kwargs["ExpressionAttributeNames"] == {"#s": "status", "#e": "error"}
        values = kwargs["ExpressionAttributeValues"]
        assert values[":new"] == "failed"
        assert values[":pending"] == "pending"
        assert values[":err"] == {"code": "ValueError", "message": "something broke"}

    def test_error_message_truncated_to_512_chars(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        handler.fail("doc-123", "BigError", "x" * 1000)

        err = fake.update_item.call_args.kwargs["ExpressionAttributeValues"][":err"]
        assert err["message"] == "x" * 512


class TestProcessRecord:
    def test_invalid_json_body_acks(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        assert handler.process_record(_record(body="{not json")) is None
        fake.put_item.assert_not_called()
        fake.update_item.assert_not_called()

    def test_missing_bucket_acks(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        body = json.dumps({"detail": {"object": {"key": _VALID_KEY}}})
        assert handler.process_record(_record(body=body)) is None
        fake.put_item.assert_not_called()

    def test_missing_key_acks(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        body = json.dumps({"detail": {"bucket": {"name": "b"}}})
        assert handler.process_record(_record(body=body)) is None
        fake.put_item.assert_not_called()

    def test_unparseable_key_acks(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)

        record = _record(body=_s3_event_body(key="garbage/key"))
        assert handler.process_record(record) is None
        fake.put_item.assert_not_called()

    def test_happy_path_claims_extracts_completes(self, monkeypatch):
        fake = MagicMock()
        fake_extract = MagicMock(
            return_value={
                "extracted_fields": {"party": ["Acme"]},
                "model_version": "g",
                "token_usage": {"input": 1, "output": 2},
                "processing_ms": 100,
            }
        )
        monkeypatch.setattr(handler, "_table", lambda: fake)
        monkeypatch.setattr(handler, "extract", fake_extract)

        assert handler.process_record(_record(body=_s3_event_body())) is None

        fake.put_item.assert_called_once()  # claim
        fake_extract.assert_called_once_with("b", _VALID_KEY, _DOC_ID)
        fake.update_item.assert_called_once()  # complete
        completed = fake.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert completed[":new"] == "succeeded"

    def test_extract_failure_marks_failed_and_returns_message_id(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)
        monkeypatch.setattr(
            handler,
            "extract",
            MagicMock(side_effect=RuntimeError("model exploded")),
        )

        record = _record(body=_s3_event_body(), message_id="msg-99")
        assert handler.process_record(record) == "msg-99"

        fake.put_item.assert_called_once()  # claim
        failed = fake.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert failed[":new"] == "failed"
        assert failed[":err"]["code"] == "RuntimeError"

    @pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
    def test_redelivery_of_terminal_doc_acks(self, monkeypatch, terminal_status):
        fake = MagicMock()
        fake.put_item.side_effect = _ccfe()
        fake.get_item.return_value = {"Item": {"status": terminal_status}}
        fake_extract = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)
        monkeypatch.setattr(handler, "extract", fake_extract)

        assert handler.process_record(_record(body=_s3_event_body())) is None
        fake_extract.assert_not_called()
        fake.update_item.assert_not_called()

    def test_pending_sibling_returns_message_for_retry(self, monkeypatch):
        fake = MagicMock()
        fake.put_item.side_effect = _ccfe()
        fake.get_item.return_value = {"Item": {"status": "pending"}}
        fake_extract = MagicMock()
        monkeypatch.setattr(handler, "_table", lambda: fake)
        monkeypatch.setattr(handler, "extract", fake_extract)

        record = _record(body=_s3_event_body(), message_id="msg-pending")
        assert handler.process_record(record) == "msg-pending"
        fake_extract.assert_not_called()
        fake.update_item.assert_not_called()

    def test_non_conditional_client_error_propagates(self, monkeypatch):
        fake = MagicMock()
        fake.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}}, "PutItem"
        )
        monkeypatch.setattr(handler, "_table", lambda: fake)

        with pytest.raises(ClientError):
            handler.process_record(_record(body=_s3_event_body()))


class TestHandler:
    def test_empty_records_returns_empty_failures(self, monkeypatch):
        fake_ls = MagicMock()
        monkeypatch.setattr(handler, "_ls_client", lambda: fake_ls)

        result = handler.handler({"Records": []}, MagicMock())

        assert result == {"batchItemFailures": []}
        fake_ls.flush.assert_called_once()

    def test_single_success_returns_empty_failures(self, monkeypatch):
        monkeypatch.setattr(handler, "process_record", lambda r: None)
        monkeypatch.setattr(handler, "_ls_client", lambda: MagicMock())

        result = handler.handler({"Records": [{"messageId": "m1"}]}, MagicMock())

        assert result == {"batchItemFailures": []}

    def test_single_failure_reports_message_id(self, monkeypatch):
        monkeypatch.setattr(handler, "process_record", lambda r: r["messageId"])
        monkeypatch.setattr(handler, "_ls_client", lambda: MagicMock())

        result = handler.handler({"Records": [{"messageId": "m1"}]}, MagicMock())

        assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_mixed_batch_only_failures_reported(self, monkeypatch):
        outcomes = iter([None, "m2", None])
        monkeypatch.setattr(handler, "process_record", lambda r: next(outcomes))
        monkeypatch.setattr(handler, "_ls_client", lambda: MagicMock())

        event = {
            "Records": [
                {"messageId": "m1"},
                {"messageId": "m2"},
                {"messageId": "m3"},
            ]
        }
        result = handler.handler(event, MagicMock())

        assert result == {"batchItemFailures": [{"itemIdentifier": "m2"}]}

    def test_flush_called_in_finally_even_when_record_handling_raises(
        self, monkeypatch
    ):
        fake_ls = MagicMock()

        def boom(record: dict[str, Any]) -> str | None:
            raise RuntimeError("oops")

        monkeypatch.setattr(handler, "process_record", boom)
        monkeypatch.setattr(handler, "_ls_client", lambda: fake_ls)

        with pytest.raises(RuntimeError):
            handler.handler({"Records": [{"messageId": "m1"}]}, MagicMock())

        fake_ls.flush.assert_called_once()

    def test_flush_failure_is_suppressed(self, monkeypatch):
        fake_ls = MagicMock()
        fake_ls.flush.side_effect = RuntimeError("langsmith api down")
        monkeypatch.setattr(handler, "process_record", lambda r: None)
        monkeypatch.setattr(handler, "_ls_client", lambda: fake_ls)

        result = handler.handler({"Records": [{"messageId": "m1"}]}, MagicMock())
        assert result == {"batchItemFailures": []}


_CACHED_GETTERS = (
    "_secrets_client",
    "_s3_client",
    "_table",
    "_bootstrap_secrets",
    "_extractor",
    "_ls_client",
)


class TestInfrastructureGetters:
    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        # Without this the @cache from a prior test (which stored a mock) would
        # be returned on a subsequent call, defeating the monkeypatches.
        for name in _CACHED_GETTERS:
            getattr(handler, name).cache_clear()
        yield
        for name in _CACHED_GETTERS:
            getattr(handler, name).cache_clear()

    def test_secrets_client_constructs_secretsmanager_client(self, monkeypatch):
        fake = MagicMock()
        factory = MagicMock(return_value=fake)
        monkeypatch.setattr(boto3, "client", factory)

        assert handler._secrets_client() is fake
        factory.assert_called_once_with("secretsmanager")

    def test_s3_client_constructs_s3_client(self, monkeypatch):
        fake = MagicMock()
        factory = MagicMock(return_value=fake)
        monkeypatch.setattr(boto3, "client", factory)

        assert handler._s3_client() is fake
        factory.assert_called_once_with("s3")

    def test_table_resolves_table_from_env(self, monkeypatch):
        fake_table_obj = MagicMock()
        fake_resource = MagicMock()
        fake_resource.Table.return_value = fake_table_obj
        monkeypatch.setattr(boto3, "resource", MagicMock(return_value=fake_resource))
        monkeypatch.setenv("RESULTS_TABLE_NAME", "my-table")

        assert handler._table() is fake_table_obj
        fake_resource.Table.assert_called_once_with("my-table")

    def test_fetch_secret_returns_secret_string(self, monkeypatch):
        fake_client = MagicMock()
        fake_client.get_secret_value.return_value = {"SecretString": "supersecret"}
        monkeypatch.setattr(handler, "_secrets_client", lambda: fake_client)

        assert handler._fetch_secret("arn:aws:secret") == "supersecret"
        fake_client.get_secret_value.assert_called_once_with(SecretId="arn:aws:secret")

    def test_bootstrap_secrets_hydrates_env_vars(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_SECRET_ARN", "arn:llm")
        monkeypatch.setenv("LANGSMITH_SECRET_ARN", "arn:ls")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        monkeypatch.setattr(
            handler, "_fetch_secret", MagicMock(side_effect=["llm-key", "ls-key"])
        )

        handler._bootstrap_secrets()

        assert os.environ["GOOGLE_API_KEY"] == "llm-key"
        assert os.environ["LANGSMITH_API_KEY"] == "ls-key"
        assert os.environ["LANGSMITH_TRACING"] == "true"

    def test_extractor_bootstraps_then_builds_single_pass(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini-fake")
        bootstrap = MagicMock()
        monkeypatch.setattr(handler, "_bootstrap_secrets", bootstrap)

        fake_model = MagicMock()
        fake_extractor_obj = MagicMock()
        model_ctor = MagicMock(return_value=fake_model)
        ext_ctor = MagicMock(return_value=fake_extractor_obj)
        monkeypatch.setattr(handler, "ChatGoogleGenerativeAI", model_ctor)
        monkeypatch.setattr(handler, "SinglePassExtractor", ext_ctor)

        assert handler._extractor() is fake_extractor_obj
        bootstrap.assert_called_once()
        model_ctor.assert_called_once_with(model="gemini-fake")
        ext_ctor.assert_called_once_with(model=fake_model, schema=NDA)

    def test_ls_client_bootstraps_then_builds_client(self, monkeypatch):
        bootstrap = MagicMock()
        monkeypatch.setattr(handler, "_bootstrap_secrets", bootstrap)

        fake_ls = MagicMock()
        ctor = MagicMock(return_value=fake_ls)
        monkeypatch.setattr(handler, "LangSmithClient", ctor)

        assert handler._ls_client() is fake_ls
        bootstrap.assert_called_once()
        ctor.assert_called_once_with()


class TestExtract:
    def test_downloads_runs_extractor_returns_structured_result(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gemini-test")
        monkeypatch.setenv("LANGSMITH_TRACING", "false")

        body = MagicMock(read=MagicMock(return_value=b"pdf-bytes"))
        fake_s3 = MagicMock()
        fake_s3.get_object.return_value = {"Body": body}
        monkeypatch.setattr(handler, "_s3_client", lambda: fake_s3)

        fake_doc = MagicMock()
        fake_loader = MagicMock()
        fake_loader.load_bytes.return_value = fake_doc
        monkeypatch.setattr(handler, "PDFLoader", MagicMock(return_value=fake_loader))

        fake_value = MagicMock()
        fake_value.model_dump.return_value = {"party": ["Acme"]}
        fake_result = MagicMock(
            value=fake_value, usage={"input_tokens": 10, "output_tokens": 20}
        )
        fake_ext = MagicMock()
        fake_ext.extract.return_value = fake_result
        monkeypatch.setattr(handler, "_extractor", lambda: fake_ext)

        result = handler.extract("bucket-x", "key-y", "doc-1")

        fake_s3.get_object.assert_called_once_with(Bucket="bucket-x", Key="key-y")
        fake_loader.load_bytes.assert_called_once_with(b"pdf-bytes", name="key-y")
        fake_ext.extract.assert_called_once_with(fake_doc)
        assert result["extracted_fields"] == {"party": ["Acme"]}
        assert result["model_version"] == "gemini-test"
        assert result["token_usage"] == {"input": 10, "output": 20}
        assert isinstance(result["processing_ms"], int)
        assert result["processing_ms"] >= 0

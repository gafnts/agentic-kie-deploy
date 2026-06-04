"""
Fixtures for the load harness (ADR-0015).

Reads the deployed infrastructure's real identifiers from Terraform, refuses
to run if that infrastructure is prod, and hands the measurement layer a single
``Targets`` bundle to work from.

Most identifiers (buckets, table, API endpoint, boto session) come from the
root ``tests/conftest.py``; this file adds the harness-only ones on top: the
main queue URL for the live sampler, the extractor and publisher function
names, and the DLQ and log-group identifiers.

Is worth noting that the prod guard resolves the environment from the live
backend rather than the ``ENV`` variable the Makefile checks, so it protects
prod no matter how the run was invoked.
"""

import re
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.conftest import _tf_output

from .measure import Targets


@pytest.fixture(scope="session")
def extraction_queue_url() -> str:
    return _tf_output("extraction_queue_url")


@pytest.fixture(scope="session")
def extractor_function_name() -> str:
    return _tf_output("extractor_function_name")


@pytest.fixture(scope="session")
def extraction_dlq_arn() -> str:
    return _tf_output("extraction_dlq_arn")


@pytest.fixture(scope="session")
def publisher_dlq_arn() -> str:
    return _tf_output("publisher_dlq_arn")


@pytest.fixture(scope="session")
def publisher_function_name() -> str:
    return _tf_output("publisher_function_name")


@pytest.fixture(scope="session")
def extractor_log_group_name() -> str:
    return _tf_output("extractor_log_group_name")


@pytest.fixture(scope="session")
def load_env(ingestion_bucket: str) -> str:
    """
    Environment parsed from resource naming, refusing prod at runtime.

    The Makefile guard only inspects the ``ENV`` variable; the harness reads
    whatever backend terraform is initialized against, so this is the guard that
    actually protects prod regardless of how the run was invoked.
    """
    match = re.match(r"agentic-kie-deploy-(?P<env>[^-]+)-ingestion", ingestion_bucket)
    env = match.group("env") if match else "unknown"
    if env == "prod":
        pytest.fail(f"load harness refuses prod (resolved from {ingestion_bucket}).")
    return env


@pytest.fixture(scope="session")
def load_targets(
    boto_session: Any,
    load_env: str,
    extraction_queue_url: str,
    extraction_dlq_arn: str,
    publisher_dlq_arn: str,
    extractor_function_name: str,
    publisher_function_name: str,
    extractor_log_group_name: str,
    results_table_name: str,
    uploader_api_endpoint: str,
    analytics_bucket: str,
    ingestion_bucket: str,
) -> Targets:
    """
    Every resource identifier the measurement layer needs, in one bundle.
    """
    return Targets(
        env=load_env,
        region=boto_session.region_name or "us-east-1",
        queue_url=extraction_queue_url,
        queue_name=extraction_queue_url.rsplit("/", 1)[-1],
        extraction_dlq_name=extraction_dlq_arn.rsplit(":", 1)[-1],
        publisher_dlq_name=publisher_dlq_arn.rsplit(":", 1)[-1],
        extractor_fn=extractor_function_name,
        publisher_fn=publisher_function_name,
        extractor_log_group=extractor_log_group_name,
        table_name=results_table_name,
        api_id=urlparse(uploader_api_endpoint).hostname.split(".")[0],  # type: ignore[union-attr]
        analytics_bucket=analytics_bucket,
        ingestion_bucket=ingestion_bucket,
    )

"""
Shared pytest fixtures for tests that touch deployed infrastructure.
"""

import subprocess
from typing import Any

import boto3
import pytest


def _tf_output(name: str) -> str:
    """
    Read a terraform output from the deployed `infra/` stack.
    """
    result = subprocess.run(
        ["terraform", "-chdir=infra", "output", "-raw", name],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def ingestion_bucket() -> str:
    return _tf_output("ingestion_bucket_name")


@pytest.fixture(scope="session")
def results_table_name() -> str:
    return _tf_output("results_table_name")


@pytest.fixture(scope="session")
def s3() -> Any:
    return boto3.client("s3")


@pytest.fixture(scope="session")
def dynamodb() -> Any:
    return boto3.resource("dynamodb")

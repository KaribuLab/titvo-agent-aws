"""Opt-in smoke of real configuration-table reads with controlled Batch states."""

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse
from uuid import uuid4

import boto3
import pytest

import code_analysis.application.analyse_code_use_case as module
from main import read_rag_indexing_timeout_seconds
from shared.infra.adapters.aws_configuration_adapter import AwsConfigurationAdapter


@pytest.fixture
def configuration_table():
    """Use a temporary table on an explicitly selected local emulator only."""
    endpoint = os.getenv("TITVO_TEST_DYNAMODB_ENDPOINT")
    if not endpoint:
        pytest.skip("Set TITVO_TEST_DYNAMODB_ENDPOINT for the local DynamoDB smoke")
    if urlparse(endpoint).hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("Smoke endpoint must be localhost or 127.0.0.1")
    client = boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    table = f"titvo-rag-timeout-smoke-{uuid4().hex}"
    client.create_table(
        TableName=table,
        AttributeDefinitions=[{"AttributeName": "parameter_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "parameter_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    try:
        client.get_waiter("table_exists").wait(TableName=table)
        provider = AwsConfigurationAdapter(client, table, MagicMock())
        yield client, table, provider
    finally:
        client.delete_table(TableName=table)
        client.close()


@pytest.mark.asyncio
async def test_table_edit_affects_next_scan_only(
    configuration_table, monkeypatch, caplog
):
    """Exercise real reads, changing deadlines, missing config and CLI exemption."""
    client, table, provider = configuration_table
    clock = SimpleNamespace(now=0.0)

    async def sleep(seconds):
        """Advance the test clock instead of waiting for a real Batch job."""
        clock.now += seconds

    def write(value):
        """Store a real string configuration record through the DynamoDB API."""
        client.put_item(
            TableName=table,
            Item={
                "parameter_id": {"S": "rag_indexing_timeout_seconds"},
                "value": {"S": value},
            },
        )

    def scan():
        """Construct a scan from a real table read with simulated job statuses."""
        budget = read_rag_indexing_timeout_seconds(provider, cli_source=False)
        trigger = MagicMock()
        trigger.get_job_status.side_effect = [
            SimpleNamespace(status="RUNNING", is_succeeded=False, is_failed=False),
            SimpleNamespace(status="SUCCEEDED", is_succeeded=True, is_failed=False),
        ]
        return module.AnalyseCodeUseCase(
            task_repository=MagicMock(),
            agent=MagicMock(),
            notification_service=MagicMock(),
            content_template="",
            rag_index_status=MagicMock(),
            rag_indexer_trigger=trigger,
            rag_indexing_timeout_seconds=budget,
        )

    monkeypatch.setattr(module, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    caplog.set_level(logging.INFO)
    write("15")
    first = scan()
    write("25")
    second = scan()
    with pytest.raises(TimeoutError, match="timeout=15s"):
        await first._wait_for_rag_job("first-job", "repo", "main")
    await second._wait_for_rag_job("second-job", "repo", "main")
    assert first.rag_indexing_timeout_seconds == 15
    assert second.rag_indexing_timeout_seconds == 25
    assert "second-job status: SUCCEEDED (elapsed=20s, timeout=25s)" in caplog.text

    client.delete_item(
        TableName=table,
        Key={"parameter_id": {"S": "rag_indexing_timeout_seconds"}},
    )
    with pytest.raises(ValueError, match="rag_indexing_timeout_seconds"):
        scan()
    assert read_rag_indexing_timeout_seconds(provider, cli_source=True) is None
    write("NaN")
    with pytest.raises(ValueError, match="rag_indexing_timeout_seconds"):
        scan()
    assert read_rag_indexing_timeout_seconds(provider, cli_source=True) is None

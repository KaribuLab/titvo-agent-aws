"""Validate table-only RAG deadlines and their composition-root wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main as entrypoint
from code_analysis.domain.entities.task_entity import TaskSource, TaskStatus


@pytest.mark.parametrize(
    "raw", [None, "", " ", "text", "0", "-1", "nan", "NaN", "inf", "-inf", "1e999"]
)
def test_invalid_timeout_fails_without_fallback(raw, monkeypatch):
    """No environment variable rescues a missing or invalid table value."""
    monkeypatch.setenv("RAG_INDEXING_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("TITVO_RAG_INDEXING_TIMEOUT_SECONDS", "1800")
    provider = MagicMock()
    provider.get_value.return_value = raw
    with pytest.raises(ValueError, match="rag_indexing_timeout_seconds"):
        entrypoint.read_rag_indexing_timeout_seconds(provider, cli_source=False)
    provider.get_value.assert_called_once_with("rag_indexing_timeout_seconds")


@pytest.mark.parametrize("raw, expected", [("1800", 1800), (" 2.5 ", 2.5)])
def test_valid_timeout_comes_from_table(raw, expected, monkeypatch):
    """Accept fractional seconds and ignore conflicting environment values."""
    monkeypatch.setenv("TITVO_RAG_INDEXING_TIMEOUT_SECONDS", "42")
    provider = MagicMock()
    provider.get_value.return_value = raw
    assert (
        entrypoint.read_rag_indexing_timeout_seconds(provider, cli_source=False)
        == expected
    )
    provider.get_secret.assert_not_called()


def test_provider_error_propagates():
    """A failed table read must not silently choose a local budget."""
    provider = MagicMock()
    provider.get_value.side_effect = RuntimeError("table unavailable")
    with pytest.raises(RuntimeError, match="table unavailable"):
        entrypoint.read_rag_indexing_timeout_seconds(provider, cli_source=False)


def test_cli_does_not_read_timeout():
    """CLI bypass applies even when reading this parameter would fail."""
    provider = MagicMock()
    provider.get_value.side_effect = AssertionError("must not read")
    assert (
        entrypoint.read_rag_indexing_timeout_seconds(provider, cli_source=True) is None
    )
    provider.get_value.assert_not_called()


@pytest.fixture
def main_runtime(monkeypatch):
    """Isolate external services while exercising the real main composition path."""
    for key in (
        "TITVO_SCAN_TASK_ID",
        "TITVO_DYNAMO_TASK_TABLE_NAME",
        "TITVO_DYNAMO_CONFIGURATION_TABLE_NAME",
        "TITVO_ENCRYPTION_KEY_NAME",
        "TITVO_BITBUCKET_CODE_INSIGHTS_FUNCTION_NAME",
        "TITVO_GITHUB_ISSUE_FUNCTION_NAME",
        "TITVO_REPORT_FUNCTION_NAME",
        "TITVO_DYNAMO_CLI_FILES_BUCKET_NAME",
        "TITVO_DYNAMO_CLI_FILES_TABLE_NAME",
    ):
        monkeypatch.setenv(key, "test")
    monkeypatch.delenv("TITVO_RAG_INDEXER_BUCKET", raising=False)
    task = SimpleNamespace(source=TaskSource.GITHUB, status=TaskStatus.COMPLETED)
    repo = MagicMock()
    repo.get_task.return_value = task
    values = {
        "rag_indexing_timeout_seconds": "1800",
        "mcp_server_url": "http://mcp",
        "ai_provider": "openai",
        "ai_model": "test",
    }
    provider = MagicMock()
    provider.get_value.side_effect = values.get
    provider.get_secret.side_effect = (
        lambda key: "test" if key == "ai_api_key" else None
    )
    for name in (
        "log_runtime_identity",
        "create_boto3_client",
        "EncryptionService",
        "AwsSecretsAdapter",
        "NotificationService",
        "LambdaBitbucketRepository",
        "LambdaGitHubRepository",
        "LambdaReportRepository",
        "CliSnapshotRepository",
        "CliRetrievalNode",
        "create_s3_rag_index_status_adapter",
        "create_rag_indexer_batch_trigger",
    ):
        monkeypatch.setattr(entrypoint, name, MagicMock())
    monkeypatch.setattr(
        entrypoint, "AwsConfigurationAdapter", MagicMock(return_value=provider)
    )
    monkeypatch.setattr(
        entrypoint, "DynamoTaskRepository", MagicMock(return_value=repo)
    )
    monkeypatch.setattr(
        entrypoint, "create_langgraph_agent", AsyncMock(return_value=(MagicMock(), ""))
    )
    use_case = MagicMock()
    use_case.execute = AsyncMock(return_value=task)
    constructor = MagicMock(return_value=use_case)
    monkeypatch.setattr(entrypoint, "AnalyseCodeUseCase", constructor)
    return task, provider, values, constructor


@pytest.mark.asyncio
async def test_main_reads_new_budget_for_each_scan(main_runtime):
    """Editing the table changes the next constructed scan, not the prior snapshot."""
    _, provider, values, constructor = main_runtime
    assert await entrypoint.main() == 0
    values["rag_indexing_timeout_seconds"] = "2400"
    assert await entrypoint.main() == 0
    assert [
        call.kwargs["rag_indexing_timeout_seconds"]
        for call in constructor.call_args_list
    ] == [1800, 2400]
    assert (
        sum(
            call.args == ("rag_indexing_timeout_seconds",)
            for call in provider.get_value.call_args_list
        )
        == 2
    )


@pytest.mark.asyncio
async def test_main_missing_budget_fails_before_job_setup(main_runtime):
    """A missing budget prevents creation of the Git trigger and analysis graph."""
    _, _, values, constructor = main_runtime
    del values["rag_indexing_timeout_seconds"]
    with pytest.raises(ValueError, match="rag_indexing_timeout_seconds"):
        await entrypoint.main()
    constructor.assert_not_called()
    entrypoint.create_rag_indexer_batch_trigger.assert_not_called()
    entrypoint.create_langgraph_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_cli_injects_none_without_reading_budget(main_runtime):
    """The real composition path bypasses both config lookup and Git adapters."""
    task, provider, values, constructor = main_runtime
    task.source = TaskSource.CLI
    values["rag_indexing_timeout_seconds"] = "invalid"
    assert await entrypoint.main() == 0
    assert constructor.call_args.kwargs["rag_indexing_timeout_seconds"] is None
    assert constructor.call_args.kwargs["rag_indexer_trigger"] is None
    assert all(
        call.args != ("rag_indexing_timeout_seconds",)
        for call in provider.get_value.call_args_list
    )
    entrypoint.create_rag_indexer_batch_trigger.assert_not_called()

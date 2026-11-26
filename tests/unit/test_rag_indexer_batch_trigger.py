"""Unit tests for RagIndexerBatchTrigger environment construction."""

from unittest.mock import MagicMock

from rag_indexer_trigger.rag_indexer_batch_trigger import RagIndexerBatchTrigger


def _trigger(**kwargs):
    defaults = {
        "batch_service": MagicMock(),
        "job_queue": "queue",
        "job_definition": "definition",
        "config_table_name": "config",
        "encryption_key_name": "key",
        "aws_stage": "",
        "aws_endpoint": "",
        "log_level": "INFO",
    }
    defaults.update(kwargs)
    return RagIndexerBatchTrigger(**defaults)


def _env_map(environment):
    return {item["name"]: item["value"] for item in environment}


def test_trigger_cli_builds_cli_environment_without_git_mode():
    batch = MagicMock()
    batch.submit_job.return_value = "job-1"
    trigger = _trigger(batch_service=batch)

    job_id = trigger.trigger_cli("https://github.com/org/repo", "batch-abc")

    assert job_id == "job-1"
    kwargs = batch.submit_job.call_args.kwargs
    assert kwargs["job_name"].startswith("rag-indexer-cli-")
    env = _env_map(kwargs["environment"])
    assert env["TITVO_SOURCE_TYPE"] == "cli"
    assert env["TITVO_SOURCE_BATCH_ID"] == "batch-abc"
    assert env["TITVO_REPO_URL"] == "https://github.com/org/repo"
    assert "TITVO_BRANCH" not in env
    assert "TITVO_COMMIT_SHA" not in env


def test_trigger_cli_includes_local_env():
    batch = MagicMock()
    batch.submit_job.return_value = "job-ls"
    trigger = _trigger(
        batch_service=batch,
        aws_stage="local",
        aws_endpoint="http://localhost:4566",
    )

    trigger.trigger_cli("https://example.org/r", "b1")
    env = _env_map(batch.submit_job.call_args.kwargs["environment"])
    assert env["AWS_STAGE"] == "local"
    assert env["AWS_ENDPOINT"] == "http://localhost:4566"
    assert env["TITVO_SOURCE_TYPE"] == "cli"
    assert "TITVO_BRANCH" not in env

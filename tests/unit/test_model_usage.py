"""Validate production-graph accounting without contacting AI or AWS services."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from code_analysis.domain.entities.task_entity import Task, TaskSource, TaskStatus
from code_analysis.domain.ports.ia_agent import AgentMessage, AsyncAgentToolsFactory
from code_analysis.infra.adapters.langgraph_agent import LangGraphAgent
from code_analysis.infra.adapters.model_usage import UsageModel


class NoTools(AsyncAgentToolsFactory):
    """Provide no network tools when analysing a registered CLI snapshot."""

    async def create_tools(self):
        return []


@pytest.mark.asyncio
async def test_production_graph_persists_usage_and_duration():
    """Count actual graph calls and persist cost beside measured coverage."""
    response = SimpleNamespace(
        content='{"issues": []}',
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "input_token_details": {"cache_read": 25},
        },
    )
    model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=response), invoke=Mock(return_value=response)
    )

    async def retrieve(state):
        return {
            "files": [{"path": "api.py", "content": "print('hello')\n"}],
            "scaned_files": 1,
        }

    agent = LangGraphAgent(
        SimpleNamespace(create_model=lambda: model),
        NoTools(),
        retrieval_node=retrieve,
        usage_provider="openai",
        usage_model="gpt-4.1-mini",
    )
    message = AgentMessage(
        role="user",
        content="{}",
        metadata={
            "extra_args": {"batch_id": "snapshot"},
            "created_at": (
                datetime.now(timezone.utc) - timedelta(seconds=5)
            ).isoformat(),
        },
    )
    result = json.loads((await agent.invoke(message)).content)
    assert result["coverage"]["complete"]
    assert (
        result["usage"]["calls"] == model.ainvoke.await_count + model.invoke.call_count
    )
    assert result["usage"]["input_tokens"] == model.ainvoke.await_count * 100
    assert result["usage"]["cost_status"] == "estimated"
    assert result["usage"]["cost_usd"] > 0
    assert result["metrics"]["task_duration_seconds"] >= 5
    assert (
        result["metrics"]["completed_batches"] == result["metrics"]["total_batches"] > 0
    )


def test_custom_endpoint_does_not_inherit_standard_provider_prices():
    """Avoid attributing OpenAI tariffs to a proxy with its own prices."""
    usage = UsageModel(
        Mock(), "real", "openai", "gpt-4.1-mini", "https://proxy.example"
    )
    assert usage.summary()["cost_status"] == "unavailable"


def test_cli_task_uses_batch_identity_without_a_commit_sha():
    """Respect the existing trigger payload for uploaded working trees."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="scan",
        result={},
        args={
            "batch_id": "snapshot",
            "repository_url": "https://example.com/team/repo",
        },
        hint_id="repo",
        scaned_files=0,
        created_at=now,
        updated_at=now,
        status=TaskStatus.PENDING,
        source=TaskSource.CLI,
    )
    assert task.commit_hash == "snapshot"
    assert task.repository_url == "https://example.com/team/repo"

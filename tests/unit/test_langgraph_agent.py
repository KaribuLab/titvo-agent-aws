"""Tests for LangGraphAgent invocation plumbing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.domain.ports.ia_agent import AgentMessage
from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ExpertRuntimeConfig,
)
from code_analysis.infra.adapters.langgraph_agent import LangGraphAgent


def _agent(config: ExpertRuntimeConfig) -> LangGraphAgent:
    agent = LangGraphAgent(
        model_factory=MagicMock(),
        tools_factory=MagicMock(),
        expert_config=config,
    )
    return agent


def _message() -> AgentMessage:
    return AgentMessage(
        role="user",
        content="Repository: https://github.com/org/repo\nCommit: abc",
        metadata={"task_id": "t-1", "scan_mode": "commit"},
    )


@pytest.mark.asyncio
async def test_breaker_is_reset_before_each_invocation():
    config = ExpertRuntimeConfig()
    config.breaker.trip("429 credit_balance_exhausted: no credits")
    agent = _agent(config)
    seen_open: list[bool] = []

    async def _ainvoke(_state, config=None):
        seen_open.append(agent._expert_config.breaker.is_open)
        return {
            "final_output": {"status": "COMPLETED", "scaned_files": 1, "issues": []},
            "expert_errors": [],
        }

    agent._workflow = MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))

    first = await agent._invoke_wrapped(_message())
    second = await agent._invoke_wrapped(_message())

    assert seen_open == [False, False]
    assert json.loads(first.content)["status"] == "COMPLETED"
    assert json.loads(second.content)["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_initial_state_carries_provider_error_channel():
    config = ExpertRuntimeConfig()
    agent = _agent(config)
    captured: dict = {}

    async def _ainvoke(state, config=None):
        captured.update(state)
        return {
            "final_output": {"status": "COMPLETED", "scaned_files": 0, "issues": []}
        }

    agent._workflow = MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))
    await agent._invoke_wrapped(_message())

    assert captured["provider_error"] is None

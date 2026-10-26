"""Offline timeout contracts against the pinned SDKs and expert public results."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from code_analysis.infra.adapters.langchain_agent_adapter import (
    LangchainAgentModelFactory,
)
from code_analysis.infra.adapters.langgraph.nodes import base_expert_node as expert_mod
from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ExpertRuntimeConfig,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
)

ENV = "TITVO_AI_REQUEST_TIMEOUT_SECONDS"
ERROR = f"{ENV} must be a finite positive number of seconds"
PROVIDERS = ["openai", "openrouter", "anthropic", "google"]
MODELS = {
    "openai": "gpt-4o",
    "openrouter": "openai/gpt-4o",
    "anthropic": "claude-3-5-sonnet",
    "google": "gemini-1.5-pro",
}


@pytest.fixture(autouse=True)
def _timeout_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("override,expected", [(None, 300.0), ("0.125", 0.125)])
def test_sdk_timeout_and_preserved_provider_contract(
    monkeypatch, provider, custom, override, expected
):
    if override is not None:
        monkeypatch.setenv(ENV, override)
    endpoint = "https://proxy.example.test/v1" if custom else None
    model = LangchainAgentModelFactory(
        provider, MODELS[provider], "synthetic-key", endpoint
    ).create_model()
    assert model.temperature == 0
    if provider in ("openai", "openrouter"):
        assert model.request_timeout == expected
        assert model.use_responses_api is (provider == "openai" and not custom)
        assert model.openai_api_base == (
            endpoint
            or ("https://openrouter.ai/api/v1" if provider == "openrouter" else None)
        )
        assert model.model_name == MODELS[provider]
        assert model.max_retries is None  # Preserve the OpenAI SDK default.
        for client in (model.root_client, model.root_async_client):
            assert client.max_retries == 2
            assert client.timeout == expected
            transport_timeout = client._client.timeout
            assert [
                transport_timeout.connect,
                transport_timeout.read,
                transport_timeout.write,
                transport_timeout.pool,
            ] == [expected] * 4
    elif provider == "anthropic":
        assert model.default_request_timeout == expected
        assert model.max_retries == 2
        assert model.anthropic_api_url == (endpoint or "https://api.anthropic.com")
        assert model.model == MODELS[provider]
        for client in (model._client, model._async_client):
            assert client.timeout == expected
            assert client.max_retries == 2
    else:
        # Pinned langchain-google-genai forwards this field as RPC seconds.
        assert model.timeout == expected
        assert model.max_retries == 6
        assert model.model == "models/" + MODELS[provider]
        assert model.client_options == ({"api_endpoint": endpoint} if custom else None)


@pytest.mark.parametrize(
    "raw", ["", " ", "no-number", "nan", "inf", "-inf", "1e999", "0", "-1"]
)
def test_invalid_timeout_fails_clearly_without_echoing_input(monkeypatch, raw):
    monkeypatch.setenv(ENV, raw)
    with pytest.raises(ValueError) as exc:
        LangchainAgentModelFactory("openai", "gpt-4o", "synthetic-key").create_model()
    assert str(exc.value) == ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "timeout", "repair_timeout"])
async def test_failed_second_batch_is_explicit_safe_and_not_clean(
    monkeypatch, caplog, failure
):
    """Use the public node delta; first batch succeeds, second must stay failed."""
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(expert_mod.asyncio, "sleep", sleep)
    private = "PRIVATE_SOURCE key=synthetic-secret https://private.example.test"
    rejected = '{"issues": [{"title": "Unsupported"}]}'
    responses = [SimpleNamespace(content='{"issues": []}')]
    if failure == "malformed":
        responses.append(SimpleNamespace(content=private))
    else:
        if failure == "repair_timeout":
            responses.append(SimpleNamespace(content=rejected))
        responses.extend([TimeoutError(private) for _ in range(3)])
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=responses))
    node = CodeVulnerabilitiesNode(
        model,
        ExpertRuntimeConfig(
            per_file_cap_chars=10000,
            batch_budget_chars=10000,
            chunk_overlap_chars=0,
        ),
    )
    with caplog.at_level(logging.INFO):
        result = await node({"files": [
            {"path": f"f{i}.py", "content": "x" * 6000, "runtimes": ["server"]}
            for i in range(2)
        ]})
    assert result["issues"] == []
    assert len(result["expert_errors"]) == 1
    assert len(result["failed_batches"]) == 1
    failed = result["failed_batches"][0]
    assert failed["batch_index"] == 1
    assert failed["paths"] == ["f1.py"]
    assert failed["expert"] == node.expert_name
    metadata = result["expert_metadata"][node.expert_name]
    assert metadata["batches_completed"] == 1
    assert metadata["failed_batches"] == 1
    if failure == "malformed":
        assert model.ainvoke.await_count == 2
        assert sleeps == []
        category = "invalid_response"
    else:
        assert model.ainvoke.await_count == (5 if failure == "repair_timeout" else 4)
        assert sleeps == [1.0, 2.0]
        category = "TimeoutError"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        node.expert_name in message and "batch 2" in message and category in message
        for message in errors
    )
    assert private not in caplog.text
    assert "synthetic-secret" not in caplog.text
    assert "https://private.example.test" not in caplog.text
    assert "Unsupported" not in caplog.text
    if failure != "malformed":
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert all(
            "batch 2" in message and "TimeoutError" in message for message in warnings
        )

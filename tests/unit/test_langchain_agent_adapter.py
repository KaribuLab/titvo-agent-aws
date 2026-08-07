"""Tests for LangchainAgentModelFactory and AIProvider multi-provider support.

These tests exercise the chat LLM model factory without calling any remote API:
they only inspect the constructed model's public attributes (model_name,
openai_api_base, openai_api_key and the use_responses_api flag).
"""

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from code_analysis.infra.adapters.langchain_agent_adapter import (
    AIProvider,
    LangchainAgentModelFactory,
)


class TestAIProviderEnum:
    """Tests for AIProvider.from_string mapping."""

    def test_openrouter_maps_to_enum(self):
        assert AIProvider.from_string("openrouter") == AIProvider.OPENROUTER

    def test_existing_providers_still_map(self):
        """Regression: openai/anthropic/google mapping unchanged."""
        assert AIProvider.from_string("openai") == AIProvider.OPENAI
        assert AIProvider.from_string("anthropic") == AIProvider.ANTHROPIC
        assert AIProvider.from_string("google") == AIProvider.GOOGLE

    def test_invalid_provider_raises_value_error(self):
        """Regression: unknown provider raises ValueError."""
        with pytest.raises(ValueError):
            AIProvider.from_string("not-a-real-provider")


class TestCreateModelOpenRouter:
    """Tests for OpenRouter provider (OpenAI-compatible Chat Completions)."""

    def test_openrouter_uses_default_base_url(self):
        factory = LangchainAgentModelFactory(
            ai_provider="openrouter",
            ai_model="anthropic/claude-3.5-sonnet",
            ai_api_key="or-key",
        )
        model = factory.create_model()
        assert isinstance(model, ChatOpenAI)
        assert model.model_name == "anthropic/claude-3.5-sonnet"
        # OpenRouter exposes OpenAI-compatible Chat Completions, never the
        # Responses API, so use_responses_api must be False.
        assert model.use_responses_api is False
        assert model.openai_api_base == "https://openrouter.ai/api/v1"
        assert model.openai_api_key.get_secret_value() == "or-key"

    def test_openrouter_with_custom_base_url(self):
        factory = LangchainAgentModelFactory(
            ai_provider="openrouter",
            ai_model="openai/gpt-4o",
            ai_api_key="or-key",
            ai_base_url="https://my-proxy.example.com/v1",
        )
        model = factory.create_model()
        assert isinstance(model, ChatOpenAI)
        assert model.openai_api_base == "https://my-proxy.example.com/v1"
        assert model.use_responses_api is False


class TestCreateModelOpenAIBaseUrl:
    """Tests for OpenAI provider with and without a custom base_url."""

    def test_openai_with_custom_base_url_disables_responses_api(self):
        """Custom OpenAI-compatible endpoints use Chat Completions, not Responses."""
        factory = LangchainAgentModelFactory(
            ai_provider="openai",
            ai_model="gpt-4o",
            ai_api_key="sk-key",
            ai_base_url="https://gateway.example.com/v1",
        )
        model = factory.create_model()
        assert isinstance(model, ChatOpenAI)
        assert model.openai_api_base == "https://gateway.example.com/v1"
        assert model.use_responses_api is False
        assert model.model_name == "gpt-4o"

    def test_openai_without_base_url_uses_responses_api(self):
        """Regression: the default OpenAI path keeps use_responses_api=True."""
        factory = LangchainAgentModelFactory(
            ai_provider="openai",
            ai_model="gpt-4o",
            ai_api_key="sk-key",
        )
        model = factory.create_model()
        assert isinstance(model, ChatOpenAI)
        assert model.use_responses_api is True
        assert model.model_name == "gpt-4o"


class TestCreateModelAnthropicAndGoogleRegression:
    """Regression tests: anthropic/google behave unchanged when ai_base_url is unset."""

    def test_anthropic_unchanged(self):
        factory = LangchainAgentModelFactory(
            ai_provider="anthropic",
            ai_model="claude-3-5-sonnet",
            ai_api_key="ant-key",
        )
        model = factory.create_model()
        assert isinstance(model, ChatAnthropic)
        assert model.model == "claude-3-5-sonnet"

    def test_google_unchanged(self):
        factory = LangchainAgentModelFactory(
            ai_provider="google",
            ai_model="gemini-1.5-pro",
            ai_api_key="g-key",
        )
        model = factory.create_model()
        assert isinstance(model, ChatGoogleGenerativeAI)
        # ChatGoogleGenerativeAI stores the model with a "models/" prefix.
        assert model.model == "models/gemini-1.5-pro"


class TestCreateModelAnthropicAndGoogleBaseUrl:
    """Tests for ai_base_url support on Anthropic and Google (custom endpoints)."""

    def test_anthropic_with_custom_base_url(self):
        factory = LangchainAgentModelFactory(
            ai_provider="anthropic",
            ai_model="claude-3-5-sonnet",
            ai_api_key="ant-key",
            ai_base_url="https://my-anthropic-proxy.example.com",
        )
        model = factory.create_model()
        assert isinstance(model, ChatAnthropic)
        assert model.anthropic_api_url == "https://my-anthropic-proxy.example.com"

    def test_google_with_custom_base_url(self):
        factory = LangchainAgentModelFactory(
            ai_provider="google",
            ai_model="gemini-1.5-pro",
            ai_api_key="g-key",
            ai_base_url="https://my-google-proxy.example.com",
        )
        model = factory.create_model()
        assert isinstance(model, ChatGoogleGenerativeAI)
        assert model.client_options == {
            "api_endpoint": "https://my-google-proxy.example.com"
        }


class TestInvalidProviderFactory:
    """Regression: invalid provider at factory level raises ValueError."""

    def test_invalid_provider_raises(self):
        factory = LangchainAgentModelFactory(
            ai_provider="nonsense",
            ai_model="m",
            ai_api_key="k",
        )
        with pytest.raises(ValueError):
            factory.create_model()

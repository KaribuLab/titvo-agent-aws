"""Tests for provider error classification and the circuit breaker."""

import httpx
import pytest

from code_analysis.infra.adapters.llm_errors import (
    ErrorClass,
    ProviderCircuitBreaker,
    classify,
    describe,
)


class _ApiError(Exception):
    """Duck-typed stand-in for any SDK's APIStatusError."""

    def __init__(self, status_code: int, message: str = "", body=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.code = code


def _openai(status: int, error: dict):
    import openai

    response = httpx.Response(
        status,
        json={"error": error},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    return openai.APIStatusError(
        f"Error code: {status} - {error}", response=response, body=error
    )


def _anthropic(status: int, error: dict):
    import anthropic

    body = {"type": "error", "error": error}
    response = httpx.Response(
        status,
        json=body,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return anthropic.APIStatusError(error["message"], response=response, body=body)


class TestClassify:
    @pytest.mark.parametrize(
        "exc",
        [
            _openai(
                429,
                {
                    "message": "You have no credits remaining.",
                    "type": "insufficient_quota",
                    "code": "credit_balance_exhausted",
                },
            ),
            _anthropic(
                400,
                {
                    "type": "invalid_request_error",
                    "message": "Your credit balance is too low to access the API.",
                },
            ),
            _ApiError(402, "Insufficient credits"),  # OpenRouter
            _openai(401, {"message": "Incorrect API key", "code": "invalid_api_key"}),
            _ApiError(403, "forbidden"),
            _ApiError(404, "model not found"),
            _ApiError(400, "unknown bad request"),
        ],
        ids=[
            "openai-no-credits",
            "anthropic-low-balance",
            "openrouter-402",
            "openai-401",
            "403",
            "404",
            "generic-400",
        ],
    )
    def test_fatal(self, exc):
        assert classify(exc) is ErrorClass.FATAL

    @pytest.mark.parametrize(
        "exc",
        [
            _openai(
                429, {"message": "Rate limit reached", "code": "rate_limit_exceeded"}
            ),
            _anthropic(
                429, {"type": "rate_limit_error", "message": "Too many requests"}
            ),
            _ApiError(429, "Resource has been exhausted"),  # Google RESOURCE_EXHAUSTED
            _ApiError(500, "internal"),
            _ApiError(503, "overloaded"),
            TimeoutError("timed out"),
            ConnectionError("reset"),
            RuntimeError("unknown"),
        ],
        ids=[
            "openai-rate-limit",
            "anthropic-rate-limit",
            "google-429",
            "500",
            "503",
            "timeout",
            "connection",
            "unknown",
        ],
    )
    def test_retry(self, exc):
        assert classify(exc) is ErrorClass.RETRY

    @pytest.mark.parametrize(
        "exc",
        [
            _openai(
                400,
                {
                    "message": "This model's maximum context length is 128000 tokens",
                    "code": "context_length_exceeded",
                },
            ),
            _anthropic(
                400, {"type": "invalid_request_error", "message": "prompt is too long"}
            ),
            _openai(400, {"message": "flagged", "code": "content_filter"}),
        ],
        ids=["openai-context-length", "anthropic-too-long", "content-filter"],
    )
    def test_fail_batch(self, exc):
        assert classify(exc) is ErrorClass.FAIL_BATCH

    def test_wrapped_cause_is_inspected(self):
        inner = _ApiError(401, "bad key")
        try:
            try:
                raise inner
            except _ApiError as exc:
                raise RuntimeError("langchain wrapper") from exc
        except RuntimeError as wrapped:
            assert classify(wrapped) is ErrorClass.FATAL
            assert describe(wrapped).startswith("401")

    def test_google_style_code_attribute(self):
        class GoogleError(Exception):
            code = 403
            status = "PERMISSION_DENIED"

        assert classify(GoogleError("denied")) is ErrorClass.FATAL

    def test_describe_uses_status_code_and_message(self):
        exc = _openai(
            429,
            {
                "message": "You have no credits remaining.",
                "type": "insufficient_quota",
                "code": "credit_balance_exhausted",
            },
        )
        assert (
            describe(exc)
            == "429 credit_balance_exhausted: You have no credits remaining."
        )

    def test_describe_without_status_falls_back_to_type(self):
        assert describe(RuntimeError("boom")) == "RuntimeError: boom"


class TestProviderCircuitBreaker:
    def test_trip_once(self):
        breaker = ProviderCircuitBreaker()
        assert not breaker.is_open
        assert breaker.trip("first") is True
        assert breaker.trip("second") is False
        assert breaker.is_open
        assert breaker.reason == "first"

    def test_reset(self):
        breaker = ProviderCircuitBreaker()
        breaker.trip("x")
        breaker.reset()
        assert not breaker.is_open
        assert breaker.reason is None

"""Account for model calls in AWS and snapshot workflows, including repair calls."""

import os
from decimal import Decimal, InvalidOperation

SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1-mini"


class UsageModel:
    """Transparent sync/async model wrapper; never infer tokens from characters."""

    def __init__(self, model, mode, provider=None, model_name=None, base_url=None):
        """Keep accounting isolated to one task and snapshot its pricing."""
        self.model = model
        self.mode = mode
        self.provider = provider
        self.model_name = model_name
        self.calls = self.measured = self.failed = 0
        self.input = self.output = self.cached = 0
        self.rates = None
        self.source = None
        overrides = [
            (os.getenv(f"TITVO_PRICE_{kind}_PER_MILLION") or None)
            for kind in ("INPUT", "CACHED", "OUTPUT")
        ]
        if any(value is not None for value in overrides):
            if not all(value is not None for value in overrides):
                raise ValueError("Configura las tres tarifas TITVO_PRICE_*_PER_MILLION")
            try:
                rates = [Decimal(value) for value in overrides]
            except InvalidOperation as exc:
                raise ValueError("Tarifas inválidas") from exc
            if any(not value.is_finite() or value < 0 for value in rates):
                raise ValueError("Tarifas deben ser finitas y positivas o cero")
            self.rates, self.source = rates, "configured"
        elif (
            provider == "openai"
            and model_name == "gpt-4.1-mini"
            and not base_url
            and not os.getenv("TITVO_AI_BASE_URL")
        ):
            self.rates = [Decimal("0.40"), Decimal("0.10"), Decimal("1.60")]
            self.source = SOURCE

    def __getattr__(self, name):
        """Forward model capabilities without changing graph behavior."""
        return getattr(self.model, name)

    def record(self, response):
        """Prefer normalized LangChain usage; accept OpenAI metadata as fallback."""
        usage = getattr(response, "usage_metadata", None) or {}
        if usage:
            incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
            cached = (usage.get("input_token_details") or {}).get("cache_read", 0)
        else:
            usage = (getattr(response, "response_metadata", None) or {}).get(
                "token_usage"
            ) or {}
            incoming, outgoing = (
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        if (
            not all(
                type(value) is int and value >= 0
                for value in (incoming, outgoing, cached)
            )
            or cached > incoming
        ):
            return
        self.measured += 1
        self.input += incoming
        self.output += outgoing
        self.cached += cached

    async def ainvoke(self, *args, **kwargs):
        """Count asynchronous expert and repair calls, including failed attempts."""
        self.calls += 1
        try:
            response = await self.model.ainvoke(*args, **kwargs)
        except Exception:
            self.failed += 1
            raise
        self.record(response)
        return response

    def invoke(self, *args, **kwargs):
        """Count synchronous consolidation and its JSON corrections."""
        self.calls += 1
        try:
            response = self.model.invoke(*args, **kwargs)
        except Exception:
            self.failed += 1
            raise
        self.record(response)
        return response

    def summary(self):
        """Mark unknown or partially recorded charges instead of claiming zero."""
        complete = self.measured == self.calls
        cost = None
        status = "unavailable"
        if self.mode == "mock":
            cost, status, complete = 0.0, "mock", True
        elif self.rates is not None and self.measured:
            cost = float(
                (
                    (self.input - self.cached) * self.rates[0]
                    + self.cached * self.rates[1]
                    + self.output * self.rates[2]
                )
                / Decimal(1000000)
            )
            status = "estimated" if complete else "partial"
        return {
            "provider": self.provider,
            "model": self.model_name,
            "calls": self.calls,
            "measured_calls": self.measured,
            "failed_calls": self.failed,
            "input_tokens": self.input,
            "cached_input_tokens": self.cached,
            "output_tokens": self.output,
            "total_tokens": self.input + self.output,
            "complete": complete,
            "cost_usd": cost,
            "cost_status": status,
            "pricing_per_million": list(map(float, self.rates)) if self.rates else None,
            "pricing_source": self.source,
            "pricing_verified_at": "2026-10-06" if self.source == SOURCE else None,
            "scope": "model_calls_only",
        }

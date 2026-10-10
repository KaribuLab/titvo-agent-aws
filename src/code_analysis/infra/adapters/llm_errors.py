"""Classification of LLM provider errors and a scan-wide circuit breaker.

Provider SDKs (openai, anthropic, google) raise different exception classes,
but every one of them exposes an HTTP status and a body or message. This
module relies on duck typing (``status_code``/``code``, ``body``, ``str(exc)``)
instead of importing any SDK, so it keeps working when a new provider is
wired in and can be unit-tested without network access.
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)

_MAX_REASON_CHARS = 300


class ErrorClass(str, Enum):
    """How a provider error must be handled."""

    RETRY = "retry"
    """Transient: rate limit, 5xx, timeouts, network. Retry with backoff."""

    FAIL_BATCH = "fail_batch"
    """This request can never succeed (too long, content filter); others can."""

    FATAL = "fatal"
    """The provider will reject every request (no credits, bad key, ...)."""


# Statuses that mean "every request will fail" regardless of the body.
# 402 is what OpenRouter returns when credits run out.
_FATAL_STATUSES = frozenset({401, 402, 403, 404})

# 400 bodies that only concern the current request.
_BATCH_FATAL_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "too many tokens",
    "content_filter",
    "content management policy",
)

# 429 bodies that are quota/billing rather than rate limiting.
_QUOTA_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "no credits remaining",
    "credit balance",
    "billing",
)


class ProviderUnavailableError(RuntimeError):
    """Raised instead of calling the provider while the breaker is open."""


def _iter_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _body_text(body: Any) -> str:
    if body is None:
        return ""
    try:
        return json.dumps(body, default=str)
    except (TypeError, ValueError):
        return str(body)


def _text_of(exc: BaseException) -> str:
    parts = [str(exc), _body_text(getattr(exc, "body", None))]
    for attr in ("code", "type"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _message_of(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    return str(exc)


def classify(exc: BaseException) -> ErrorClass:
    """Classify *exc* (or any exception in its cause chain)."""
    for item in _iter_chain(exc):
        status = _status_of(item)
        if status is None:
            continue
        text = _text_of(item)
        if status == 400:
            if any(marker in text for marker in _BATCH_FATAL_MARKERS):
                return ErrorClass.FAIL_BATCH
            return ErrorClass.FATAL
        if status in _FATAL_STATUSES:
            return ErrorClass.FATAL
        if status == 429:
            if any(marker in text for marker in _QUOTA_MARKERS):
                return ErrorClass.FATAL
            return ErrorClass.RETRY
        return ErrorClass.RETRY
    return ErrorClass.RETRY


def describe(exc: BaseException) -> str:
    """Short, log-safe description: ``<status> [<code>]: <message>``."""
    for item in _iter_chain(exc):
        status = _status_of(item)
        if status is None:
            continue
        label = str(status)
        code = getattr(item, "code", None)
        if isinstance(code, str) and code:
            label += f" {code}"
        return f"{label}: {_message_of(item)}"[:_MAX_REASON_CHARS]
    return f"{type(exc).__name__}: {exc}"[:_MAX_REASON_CHARS]


@dataclass
class ProviderCircuitBreaker:
    """Scan-wide latch: once open, nobody calls the provider again.

    Shared by every expert node of a workflow (they run concurrently in one
    event loop, so no locking is needed). Reset at the start of each scan.
    """

    _reason: str | None = field(default=None, init=False, repr=False)

    @property
    def is_open(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    def trip(self, reason: str) -> bool:
        """Open the breaker. Returns True only for the first trip."""
        if self._reason is not None:
            return False
        self._reason = reason
        return True

    def reset(self) -> None:
        self._reason = None

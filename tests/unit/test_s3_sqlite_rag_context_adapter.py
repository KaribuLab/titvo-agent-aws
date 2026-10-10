"""Tests for S3SqliteRagContextAdapter embedding failure handling."""

from unittest.mock import MagicMock

import pytest

from code_analysis.infra.adapters.s3_sqlite_rag_context_adapter import (
    S3SqliteRagContextAdapter,
)


class _ProviderError(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body = {"message": message, "code": code}


@pytest.fixture
def adapter(monkeypatch):
    instance = S3SqliteRagContextAdapter(
        s3_client=MagicMock(),
        bucket_name="bucket",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_api_key="key",
    )
    monkeypatch.setattr(instance, "_ensure_db", lambda: "/tmp/fake.db")
    monkeypatch.setattr(instance, "_query_db", lambda *_args, **_kw: [])
    instance.configure("https://github.com/org/repo", "main")
    return instance


def test_fatal_embedding_error_disables_provider_for_the_scan(adapter):
    embeddings = MagicMock()
    embeddings.embed_documents.side_effect = _ProviderError(
        429, "You have no credits remaining.", code="insufficient_quota"
    )
    adapter._embeddings = embeddings

    results = [adapter.search(f"query {i}", k=3) for i in range(10)]

    assert results == [[]] * 10
    assert embeddings.embed_documents.call_count == 1


def test_transient_embedding_error_does_not_disable_provider(adapter):
    embeddings = MagicMock()
    embeddings.embed_documents.side_effect = [
        TimeoutError("timed out"),
        [[0.1, 0.2]],
    ]
    adapter._embeddings = embeddings

    assert adapter.search("first", k=3) == []
    adapter.search("second", k=3)

    assert embeddings.embed_documents.call_count == 2


def test_configure_clears_the_disabled_state(adapter):
    embeddings = MagicMock()
    embeddings.embed_documents.side_effect = _ProviderError(401, "bad key")
    adapter._embeddings = embeddings
    adapter.search("q", k=1)
    assert embeddings.embed_documents.call_count == 1

    adapter.configure("https://github.com/org/repo", "main")
    embeddings.embed_documents.side_effect = None
    embeddings.embed_documents.return_value = [[0.1]]
    adapter.search("q", k=1)

    assert embeddings.embed_documents.call_count == 2

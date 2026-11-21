"""Unit tests for S3 RAG index status checks (branch and CLI layouts)."""

from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from code_analysis.infra.adapters.s3_rag_index_status_adapter import (
    S3RagIndexStatusAdapter,
)


def _client_error(code: str):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": code}}, "HeadObject"
    )


def test_is_cli_indexed_heads_cli_latest_meta():
    s3 = MagicMock()
    adapter = S3RagIndexStatusAdapter(s3, "rag-bucket")
    assert adapter.is_cli_indexed("https://github.com/org/repo.git") is True
    s3.head_object.assert_called_once_with(
        Bucket="rag-bucket",
        Key="github.com/org/repo/cli/latest/meta.json",
    )


def test_is_cli_indexed_missing_returns_false():
    s3 = MagicMock()
    s3.head_object.side_effect = _client_error("404")
    adapter = S3RagIndexStatusAdapter(s3, "rag-bucket")
    assert adapter.is_cli_indexed("https://github.com/org/repo") is False


def test_is_indexed_uses_branch_layout():
    s3 = MagicMock()
    adapter = S3RagIndexStatusAdapter(s3, "rag-bucket")
    assert adapter.is_indexed("https://github.com/org/repo", "main") is True
    s3.head_object.assert_called_once_with(
        Bucket="rag-bucket",
        Key="github.com/org/repo/branches/main/latest/meta.json",
    )


def test_is_cli_indexed_propagates_non_404():
    s3 = MagicMock()
    s3.head_object.side_effect = _client_error("403")
    adapter = S3RagIndexStatusAdapter(s3, "rag-bucket")
    with pytest.raises(botocore.exceptions.ClientError):
        adapter.is_cli_indexed("https://github.com/org/repo")

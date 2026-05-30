"""Tests for RagRetrievalNode."""

from unittest.mock import MagicMock

import pytest

from code_analysis.domain.ports.rag_context_port import IRagContextPort
from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
    RagRetrievalNode,
)


def _make_state(**kwargs):
    base = {
        "task_id": "task-1",
        "repository_url": "https://github.com/org/repo",
        "branch": "main",
        "commit_hash": "abc123",
        "extra_args": {},
        "files": [],
        "scaned_files": 0,
        "issues": [],
    }
    base.update(kwargs)
    return base


class MockRagContextPort(IRagContextPort):
    def __init__(self, search_results=None, raise_on_search=False):
        self._results = search_results or []
        self._raise_on_search = raise_on_search
        self.configured_url = None
        self.configured_branch = None

    def configure(self, repository_url: str, branch: str) -> None:
        self.configured_url = repository_url
        self.configured_branch = branch

    def search(self, query: str, k: int):
        if self._raise_on_search:
            raise RuntimeError("search error")
        return self._results[:k]

    def close(self) -> None:
        pass


class TestRagRetrievalNode:
    """Tests for RagRetrievalNode."""

    @pytest.fixture
    def chunks(self):
        return [
            {
                "file_path": "src/auth.py",
                "chunk_text": "def authenticate(): pass",
                "distance": 0.1,
            },
            {
                "file_path": "src/utils.py",
                "chunk_text": "def helper(): pass",
                "distance": 0.2,
            },
            {
                "file_path": "src/models.py",
                "chunk_text": "class User: pass",
                "distance": 0.3,
            },
        ]

    @pytest.mark.asyncio
    async def test_successful_retrieval(self, chunks):
        """Should retrieve and deduplicate chunks from files in state."""
        mock_port = MockRagContextPort(search_results=chunks)
        node = RagRetrievalNode(mock_port)

        state = _make_state(
            files=[
                {"path": "src/main.py", "content": "import auth"},
            ]
        )
        result = await node(state)

        assert "rag_chunks" in result
        assert len(result["rag_chunks"]) > 0
        assert mock_port.configured_url == "https://github.com/org/repo"
        assert mock_port.configured_branch == "main"

    @pytest.mark.asyncio
    async def test_empty_files_returns_empty_chunks(self):
        """Should return rag_chunks=[] when no files in state."""
        mock_port = MockRagContextPort()
        node = RagRetrievalNode(mock_port)

        state = _make_state(files=[])
        result = await node(state)

        assert result == {"rag_chunks": []}

    @pytest.mark.asyncio
    async def test_no_files_key_returns_empty_chunks(self):
        """Should return rag_chunks=[] when files key is missing."""
        mock_port = MockRagContextPort()
        node = RagRetrievalNode(mock_port)

        state = _make_state()
        del state["files"]
        result = await node(state)

        assert result == {"rag_chunks": []}

    @pytest.mark.asyncio
    async def test_search_error_returns_empty_chunks(self):
        """Should return rag_chunks=[] gracefully when search raises."""
        mock_port = MockRagContextPort(raise_on_search=True)
        node = RagRetrievalNode(mock_port)

        state = _make_state(
            files=[
                {"path": "src/app.py", "content": "print('hello')"},
            ]
        )
        result = await node(state)

        assert result == {"rag_chunks": []}

    @pytest.mark.asyncio
    async def test_deduplication(self, chunks):
        """Same chunk_text from multiple file queries should appear only once."""
        # Return same chunk for every file query
        same_chunk = [
            {"file_path": "shared.py", "chunk_text": "shared code", "distance": 0.1}
        ]
        mock_port = MockRagContextPort(search_results=same_chunk)
        node = RagRetrievalNode(mock_port)

        state = _make_state(
            files=[
                {"path": "src/a.py", "content": "code a"},
                {"path": "src/b.py", "content": "code b"},
            ]
        )
        result = await node(state)

        texts = [c["chunk_text"] for c in result["rag_chunks"]]
        assert len(texts) == len(set(texts)), (
            "Duplicate chunk_text should be deduplicated"
        )

    @pytest.mark.asyncio
    async def test_max_total_chunks_limit(self):
        """Should not exceed _MAX_CHUNKS_TOTAL=30 chunks."""
        many_chunks = [
            {"file_path": f"src/f{i}.py", "chunk_text": f"chunk {i}", "distance": 0.1}
            for i in range(100)
        ]
        mock_port = MockRagContextPort(search_results=many_chunks)
        node = RagRetrievalNode(mock_port)

        files = [
            {"path": f"src/file{i}.py", "content": f"content {i}"} for i in range(20)
        ]
        state = _make_state(files=files)
        result = await node(state)

        assert len(result["rag_chunks"]) <= 30

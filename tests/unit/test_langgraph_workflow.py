"""Tests for LangGraph workflow builder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState


class TestMCPRetrievalNode:
    """Tests for MCP retrieval node."""

    @pytest.fixture
    def mock_mcp_client(self):
        client = MagicMock()
        client.get_tools = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def node(self, mock_mcp_client):
        return MCPRetrievalNode(mock_mcp_client)

    def test_sanitize_tool_name(self, node):
        """Tool names should be sanitized."""
        assert node._sanitize_tool_name("git.commit-files") == "git_commit_files"
        assert node._sanitize_tool_name("test.tool-name") == "test_tool_name"

    def test_extract_file_paths_from_dict(self, node):
        """Should extract file paths from dict result."""
        result = {"files": ["src/a.py", "src/b.py"]}
        paths = node._extract_file_paths(result)
        assert paths == ["src/a.py", "src/b.py"]

    def test_extract_file_paths_from_list(self, node):
        """Should extract file paths from list."""
        result = ["src/a.py", "src/b.py"]
        paths = node._extract_file_paths(result)
        assert paths == ["src/a.py", "src/b.py"]

    def test_extract_file_paths_from_poll_success_payload(self, node):
        """MCP git.commit-files.poll exposes filesPaths en el root o bajo data."""
        top = {"status": "SUCCESS", "filesPaths": ["sha/a.ts", "sha/b.ts"]}
        assert node._extract_file_paths(top) == ["sha/a.ts", "sha/b.ts"]

        nested = {
            "status": "SUCCESS",
            "data": {"filesPaths": ["x/1.py"], "commitId": "abc"},
        }
        assert node._extract_file_paths(nested) == ["x/1.py"]

    def test_extract_job_id(self, node):
        assert node._extract_job_id({"jobId": "mcp.tool.git.commit-files-abc"}) == (
            "mcp.tool.git.commit-files-abc"
        )
        assert node._extract_job_id({"job_id": "legacy"}) == "legacy"
        assert node._extract_job_id({}) is None

    def test_extract_file_content_from_dict(self, node):
        """Should extract content from dict."""
        result = {"content": "print('hello')"}
        content = node._extract_file_content(result)
        assert content == "print('hello')"

    def test_extract_file_content_from_string(self, node):
        """Should return string directly."""
        result = "print('hello')"
        content = node._extract_file_content(result)
        assert content == "print('hello')"


class TestMergeFindingsNode:
    """Tests for merge findings node."""

    @pytest.fixture
    def node(self):
        return MergeFindingsNode()

    def test_empty_issues_returns_completed(self, node):
        """No issues should return COMPLETED status."""
        state: AgentState = {
            "task_id": "test",
            "repository_url": "",
            "commit_hash": "",
            "extra_args": {},
            "files": [],
            "scaned_files": 0,
            "issues": [],
        }
        result = node(state)
        assert result["status"] == "COMPLETED"

    def test_critical_issues_returns_failed(self, node):
        """Critical issues should return FAILED status."""
        from code_analysis.domain.entities.expert_result import ExpertIssue

        issue = ExpertIssue(
            title="RCE",
            description="Remote code execution",
            severity="CRITICAL",
            category="RCE",
            path="src/app.py",
            line=1,
            summary="RCE",
            code="eval(x)",
            recommendation="Remove eval",
        )
        state: AgentState = {
            "task_id": "test",
            "repository_url": "",
            "commit_hash": "",
            "extra_args": {},
            "files": [],
            "scaned_files": 5,
            "issues": [issue],
        }
        result = node(state)
        assert result["status"] == "FAILED"

    def test_medium_issues_returns_warning(self, node):
        """Medium issues should return WARNING status."""
        from code_analysis.domain.entities.expert_result import ExpertIssue

        issue = ExpertIssue(
            title="Verbose Error",
            description="Info disclosure",
            severity="MEDIUM",
            category="Info",
            path="src/errors.py",
            line=10,
            summary="Verbose",
            code="str(e)",
            recommendation="Use generic",
        )
        state: AgentState = {
            "task_id": "test",
            "repository_url": "",
            "commit_hash": "",
            "extra_args": {},
            "files": [],
            "scaned_files": 3,
            "issues": [issue],
        }
        result = node(state)
        assert result["status"] == "WARNING"

    def test_deduplication_in_merge(self, node):
        """Duplicate issues should be deduplicated."""
        from code_analysis.domain.entities.expert_result import ExpertIssue

        # Same issue twice
        issue1 = ExpertIssue(
            title="XSS",
            description="XSS",
            severity="HIGH",
            category="XSS",
            path="src/app.py",
            line=10,
            summary="XSS",
            code="innerHTML",
            recommendation="Escape",
        )
        issue2 = ExpertIssue(
            title="XSS2",  # Different title
            description="XSS2",
            severity="HIGH",
            category="XSS",  # Same category
            path="src/app.py",  # Same path
            line=10,  # Same line
            summary="XSS2",
            code="innerHTML",
            recommendation="Escape",
        )
        state: AgentState = {
            "task_id": "test",
            "repository_url": "",
            "commit_hash": "",
            "extra_args": {},
            "files": [],
            "scaned_files": 5,
            "issues": [issue1, issue2],
        }
        result = node(state)
        
        # Should be deduplicated to 1 issue
        final_output = result.get("final_output", {})
        assert len(final_output.get("issues", [])) == 1


class TestAgentState:
    """Tests for AgentState TypedDict."""

    def test_state_creation(self):
        """AgentState should accept required fields."""
        state: AgentState = {
            "task_id": "abc123",
            "repository_url": "https://github.com/test/repo",
            "commit_hash": "abc123def",
            "extra_args": {"key": "value"},
            "files": [],
            "scaned_files": 0,
            "issues": [],
        }
        assert state["task_id"] == "abc123"
        assert state["repository_url"] == "https://github.com/test/repo"

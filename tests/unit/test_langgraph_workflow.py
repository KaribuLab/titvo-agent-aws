"""Tests for LangGraph workflow builder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.domain.ports.rag_context_port import IRagContextPort
from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)
from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
    RagRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState
from code_analysis.infra.adapters.langgraph.workflow import LangGraphWorkflowBuilder


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

    def test_extract_and_normalize_storage_prefix(self, node):
        payload = {
            "status": "SUCCESS",
            "filesPaths": ["full/job-1/src/app.py"],
            "storagePrefix": "full/job-1",
        }

        assert node._extract_storage_prefix(payload) == "full/job-1"
        assert node._normalize_storage_path(
            "full/job-1/src/app.py", "full/job-1"
        ) == "src/app.py"

    @pytest.mark.asyncio
    async def test_full_scan_invokes_mcp_with_scan_mode_and_normalizes_paths(self):
        git_tool = MagicMock()
        git_tool.name = "mcp.tool.git.commit-files"
        git_tool.ainvoke = AsyncMock(return_value={"jobId": "job-1"})

        poll_tool = MagicMock()
        poll_tool.name = "mcp.tool.git.commit-files.poll"
        poll_tool.ainvoke = AsyncMock(
            return_value={
                "status": "SUCCESS",
                "filesPaths": ["full/job-1/src/app.py"],
                "storagePrefix": "full/job-1",
            }
        )

        files_tool = MagicMock()
        files_tool.name = "mcp.tool.files"
        files_tool.ainvoke = AsyncMock(return_value={"content": "print('hello')"})

        client = MagicMock()
        client.get_tools = AsyncMock(return_value=[git_tool, poll_tool, files_tool])

        node = MCPRetrievalNode(client)
        result = await node(
            {
                "task_id": "task-1",
                "repository_url": "https://github.com/org/repo",
                "branch": "main",
                "commit_hash": "abc123",
                "extra_args": {},
                "scan_mode": "full",
                "scan_ref": "main",
                "files": [],
                "scaned_files": 0,
                "issues": [],
            }
        )

        git_tool.ainvoke.assert_awaited_once_with(
            {
                "repository": "https://github.com/org/repo",
                "commitId": "abc123",
                "scanMode": "full",
                "branch": "main",
            }
        )
        files_tool.ainvoke.assert_awaited_once_with(
            {"path": "full/job-1/src/app.py"}
        )
        assert result["files"] == [{"path": "src/app.py", "content": "print('hello')"}]


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
        """AgentState should accept required fields including branch."""
        state: AgentState = {
            "task_id": "abc123",
            "repository_url": "https://github.com/test/repo",
            "branch": "main",
            "commit_hash": "abc123def",
            "extra_args": {"key": "value"},
            "files": [],
            "scaned_files": 0,
            "issues": [],
        }
        assert state["task_id"] == "abc123"
        assert state["repository_url"] == "https://github.com/test/repo"
        assert state["branch"] == "main"

    def test_rag_chunks_is_optional(self):
        """rag_chunks should be optional (NotRequired) in AgentState."""
        state: AgentState = {
            "task_id": "abc123",
            "repository_url": "https://github.com/test/repo",
            "branch": "main",
            "commit_hash": "abc123def",
            "extra_args": {},
            "files": [],
            "scaned_files": 0,
            "issues": [],
        }
        # rag_chunks is NotRequired, should default to missing
        assert state.get("rag_chunks") is None


class TestLangGraphWorkflowWithRag:
    """Integration-style tests for workflow with RagRetrievalNode."""

    @pytest.fixture
    def mock_mcp_client(self):
        client = MagicMock()
        client.get_tools = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_model(self):
        return MagicMock()

    @pytest.fixture
    def mock_rag_port(self):
        class _MockPort(IRagContextPort):
            def configure(self, repo, branch):
                pass

            def search(self, query, k):
                return []

            def close(self):
                pass

        return _MockPort()

    def test_workflow_includes_rag_retrieve_node(
        self, mock_mcp_client, mock_model, mock_rag_port
    ):
        """Workflow built with a RagRetrievalNode should include 'rag_retrieve' node."""
        rag_node = RagRetrievalNode(mock_rag_port)
        builder = LangGraphWorkflowBuilder(
            mock_mcp_client, mock_model, rag_node=rag_node
        )
        workflow = builder.build()

        # The compiled graph should expose node names
        nodes = list(workflow.get_graph().nodes.keys())
        assert "rag_retrieve" in nodes, f"Expected 'rag_retrieve' in {nodes}"
        assert "expert_owasp_mobile" in nodes

    def test_workflow_without_rag_node_excludes_rag_retrieve(
        self, mock_mcp_client, mock_model
    ):
        """Workflow built without RagRetrievalNode should NOT include 'rag_retrieve'."""
        builder = LangGraphWorkflowBuilder(mock_mcp_client, mock_model, rag_node=None)
        workflow = builder.build()

        nodes = list(workflow.get_graph().nodes.keys())
        assert "rag_retrieve" not in nodes, f"'rag_retrieve' should not be in {nodes}"
        assert "expert_owasp_mobile" in nodes

    def test_workflow_chains_owasp_mobile_between_web_and_devsecops(
        self, mock_mcp_client, mock_model
    ):
        """OWASP Mobile should run after web and before DevSecOps."""
        builder = LangGraphWorkflowBuilder(mock_mcp_client, mock_model, rag_node=None)
        workflow = builder.build()

        edges = workflow.get_graph().edges
        edge_pairs = {(edge.source, edge.target) for edge in edges}

        assert ("expert_owasp_web", "expert_owasp_mobile") in edge_pairs
        assert ("expert_owasp_mobile", "expert_devsecops") in edge_pairs

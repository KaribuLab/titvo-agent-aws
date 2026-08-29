"""Regression harness for SC-001 (specs/001-reproducible-scan/spec.md).

Runs the same commit through the compiled LangGraph workflow multiple times
using a deterministic model double (same input -> same output, simulating
temperature=0 sampling without calling a real provider) and asserts the
final result is byte-for-byte identical across runs.

This guards the workflow's OWN aggregation logic (state accumulation across
sequential expert nodes, findings consolidation) against non-determinism
that would exist independently of the LLM's own sampling behavior -- the
two known root causes (temperature not enforced, truncation budget coupled
to commit size) are covered separately by test_langchain_agent_adapter.py
and test_base_expert_node.py.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.infra.adapters.langgraph.workflow import LangGraphWorkflowBuilder

_ISSUE = {
    "title": "Hardcoded secret access",
    "description": "Secret pulled from environment and printed to stdout.",
    "severity": "HIGH",
    "category": "secrets",
    "path": "src/app.py",
    "line": 2,
    "summary": "Secret leaked via print.",
    "code": "print(os.environ['SECRET'])",
    "recommendation": "Do not print secrets to stdout.",
}
_EXPERT_RESPONSE = json.dumps({"issues": [_ISSUE]})
_CONSOLIDATED_RESPONSE = json.dumps({"issues": [_ISSUE]})


class _CannedModel:
    """Deterministic model double: always answers the same way, regardless
    of which expert prompt it was called with."""

    async def ainvoke(self, messages):
        return SimpleNamespace(content=_EXPERT_RESPONSE)

    def invoke(self, messages):
        return SimpleNamespace(content=_CONSOLIDATED_RESPONSE)


def _make_mcp_client():
    files_by_path = {
        "src/app.py": "import os\nprint(os.environ['SECRET'])\n",
        "src/util.py": "def helper():\n    pass\n",
    }

    git_tool = MagicMock()
    git_tool.name = "mcp.tool.git.commit-files"
    git_tool.ainvoke = AsyncMock(return_value={"jobId": "job-1"})

    poll_tool = MagicMock()
    poll_tool.name = "mcp.tool.git.commit-files.poll"
    poll_tool.ainvoke = AsyncMock(
        return_value={"status": "SUCCESS", "filesPaths": list(files_by_path)}
    )

    files_tool = MagicMock()
    files_tool.name = "mcp.tool.files"

    async def _read_file(args):
        return {"content": files_by_path[args["path"]]}

    files_tool.ainvoke = AsyncMock(side_effect=_read_file)

    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[git_tool, poll_tool, files_tool])
    return client


async def _run_scan():
    workflow = LangGraphWorkflowBuilder(
        _make_mcp_client(), _CannedModel(), rag_node=None
    ).build()
    initial_state = {
        "task_id": "task-1",
        "repository_url": "https://github.com/org/repo",
        "branch": "main",
        "commit_hash": "abc123",
        "extra_args": {},
        "scan_mode": "commit",
        "scan_ref": "main",
        "files": [],
        "scaned_files": 0,
        "issues": [],
        "current_expert_index": 0,
        "expert_errors": [],
    }
    result = await workflow.ainvoke(initial_state)
    return result["final_output"]


class TestReproducibleScan:
    """SC-001: re-scanning the same commit must produce identical findings."""

    @pytest.mark.asyncio
    async def test_same_commit_scanned_three_times_yields_identical_findings(self):
        outputs = [await _run_scan() for _ in range(3)]

        assert outputs[0]["status"] == "FAILED"  # HIGH severity issue present
        assert outputs[0]["scaned_files"] == 2
        assert len(outputs[0]["issues"]) == 1

        for output in outputs[1:]:
            assert output == outputs[0]

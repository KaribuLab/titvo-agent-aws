"""Public retrieval regressions for the root policy read boundary."""

import errno
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)


async def retrieve(mode, path, response, storage_prefix=None):
    payload = {"status": "SUCCESS", "filesPaths": ["src/good.py", path]}
    if storage_prefix:
        payload["storagePrefix"] = storage_prefix
    tools = []
    for name, value in [
        ("git.commit-files", {"jobId": "job"}),
        ("git.commit-files.poll", payload),
        ("files", None),
    ]:
        tool = MagicMock()
        tool.name = name
        tool.ainvoke = AsyncMock(return_value=value)
        tools.append(tool)
    tools[-1].ainvoke.side_effect = [{"content": "code"}, response]
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=tools)
    result = await MCPRetrievalNode(client)(
        {"repository_url": "https://example.invalid/repo", "commit_hash": "sha", "scan_mode": mode}
    )
    assert tools[-1].ainvoke.call_count == 2
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["commit", "full"])
@pytest.mark.parametrize("path", ["nested/.titvoignore.yml", "other/.titvoignore.yml", "sha/nested/.titvoignore.yml", "snapshot/nested/.titvoignore.yml", "src/bad.py"])
@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO, None])
async def test_non_policy_read_failure_is_best_effort(mode, path, failure, caplog):
    response = OSError(failure, "read failed") if failure else {"content": None}
    result = await retrieve(mode, path, response, "snapshot" if mode == "full" else None)
    assert result == {"files": [{"path": "src/good.py", "content": "code"}], "scaned_files": 1, "mcp_error": None}
    if failure:
        assert f"Failed to read file {path}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,prefix,path", [
    ("commit", None, ".titvoignore.yml"),
    ("commit", None, "sha/.titvoignore.yml"),
    ("full", "snapshot", ".titvoignore.yml"),
    ("full", "snapshot", "snapshot/.titvoignore.yml"),
])
@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO, None])
async def test_exact_policy_read_failure_is_fatal(mode, prefix, path, failure):
    response = OSError(failure, "read failed") if failure else {"content": None}
    assert await retrieve(mode, path, response, prefix) == {
        "mcp_error": "Could not read .titvoignore.yml", "status": "FAILED", "scaned_files": 0, "files": []
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,prefix,path", [
    ("commit", None, "sha/.titvoignore.yml"),
    ("full", "snapshot", "snapshot/.titvoignore.yml"),
])
async def test_valid_policy_and_source_paths_preserved(mode, prefix, path):
    policy = "version: 1\nexceptions: []\n"
    result = await retrieve(mode, path, {"content": policy}, prefix)
    assert result == {"files": [{"path": "src/good.py", "content": "code"}], "scaned_files": 1, "mcp_error": None, "accepted_exceptions_text": policy}

"""File transport normalization through the public retrieval node, without MCP I/O."""

import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_mcp_adapters.tools import _convert_call_tool_result
from mcp.types import CallToolResult, TextContent

from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)

SOURCE = 'first\r\npattern = r"\\n"\r\nlast\n'
JSON_SOURCE = '{\n  "content": "literal\\nvalue",\n  "enabled": true\n}\n'


def adapter_text(*texts):
    """Use the installed locked adapter's actual text result conversion."""
    result, _ = _convert_call_tool_result(
        CallToolResult(content=[TextContent(type="text", text=text) for text in texts])
    )
    return result


def envelope(source):
    return json.dumps({"content": source, "contentType": "text/plain"})


async def retrieve(payload):
    git_tool = MagicMock(name="git tool")
    git_tool.name = "mcp.tool.git.commit-files"
    git_tool.ainvoke = AsyncMock(return_value=json.dumps({"jobId": "job-1"}))
    poll_tool = MagicMock(name="poll tool")
    poll_tool.name = "mcp.tool.git.commit-files.poll"
    poll_tool.ainvoke = AsyncMock(
        return_value=json.dumps(
            {"status": "SUCCESS", "filesPaths": ["sha/src/app.py"]}
        )
    )
    files_tool = MagicMock(name="files tool")
    files_tool.name = "mcp.tool.files"
    files_tool.ainvoke = AsyncMock(return_value=payload)
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[git_tool, poll_tool, files_tool])
    state = {
        "repository_url": "https://example.invalid/repo",
        "commit_hash": "sha",
        "files": [],
        "scaned_files": 0,
    }
    before = copy.deepcopy(state)
    result = await MCPRetrievalNode(client)(state)
    assert state == before
    git_tool.ainvoke.assert_awaited_once_with(
        {"repository": state["repository_url"], "commitId": "sha", "scanMode": "commit"}
    )
    poll_tool.ainvoke.assert_awaited_once_with({"jobId": "job-1"})
    files_tool.ainvoke.assert_awaited_once_with({"path": "sha/src/app.py"})
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [SOURCE, JSON_SOURCE, envelope(SOURCE), ""])
async def test_gateway_envelope_is_decoded_once_preserving_source(source):
    result = await retrieve(adapter_text(envelope(source)))
    assert result == {
        "files": [{"path": "sha/src/app.py", "content": source}],
        "scaned_files": 1,
        "mcp_error": None,
    }


@pytest.mark.asyncio
async def test_single_text_list_preserves_one_file():
    result = await retrieve([envelope(SOURCE)])
    assert result == {
        "files": [{"path": "sha/src/app.py", "content": SOURCE}],
        "scaned_files": 1,
        "mcp_error": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        adapter_text(envelope(SOURCE), envelope("unrelated\nfile\n")),
        adapter_text("first text", "second text"),
        {"content": 42},
        {"file_content": ["not source"]},
        envelope(42),
    ],
)
async def test_ambiguous_or_nonstring_content_is_not_stored(payload):
    assert await retrieve(payload) == {
        "mcp_error": "No files could be read from commit",
        "status": "FAILED",
        "scaned_files": 0,
        "files": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        SOURCE,
        JSON_SOURCE,
        '{"content": "ordinary JSON source"}',
        '{"content": "ordinary JSON", "contentType": "application setting"}',
        '{"content": "ordinary JSON", "contentType": "text/plain", "other": true}',
        '"JSON string literal\\nnot source to decode"',
        '{"content": malformed JSON',
    ],
)
async def test_raw_source_is_not_unescaped_or_stripped(source):
    assert await retrieve(adapter_text(source)) == {
        "files": [{"path": "sha/src/app.py", "content": source}],
        "scaned_files": 1,
        "mcp_error": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["content", "file_content"])
async def test_existing_dictionary_strings_remain_supported(key):
    assert await retrieve({key: SOURCE}) == {
        "files": [{"path": "sha/src/app.py", "content": SOURCE}],
        "scaned_files": 1,
        "mcp_error": None,
    }

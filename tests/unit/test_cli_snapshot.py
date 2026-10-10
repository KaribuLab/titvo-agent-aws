"""Exercise snapshot integrity and source routing independently of AWS."""

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_analysis.infra.adapters.cli_snapshot import (
    CliSnapshotRepository,
    read_snapshot,
)
from code_analysis.infra.adapters.langgraph.nodes.cli_retrieval_node import (
    CliRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
)
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)
from code_analysis.infra.adapters.langgraph.workflow import LangGraphWorkflowBuilder


def archive(entries):
    """Construct test packages, including intentionally unsafe names."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, data in entries:
            member = tarfile.TarInfo(path)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def test_manifest_corruption_and_missing_file_fail():
    data = b"print('hello')"
    manifest = json.dumps(
        {
            "files": [
                {
                    "path": "src/app.py",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ]
        }
    ).encode()
    assert (
        read_snapshot(
            archive([("src/app.py", data), (".titvo-manifest.json", manifest)])
        )[0]["content"]
        == data.decode()
    )
    for entries in [
        [("src/app.py", b"corrupt"), (".titvo-manifest.json", manifest)],
        [(".titvo-manifest.json", manifest)],
    ]:
        with pytest.raises(ValueError):
            read_snapshot(archive(entries))


@pytest.mark.parametrize("path", ["../../escape.py", "/absolute.py", "x\\escape.py"])
def test_unsafe_archive_paths_fail(path):
    with pytest.raises(ValueError):
        read_snapshot(archive([(path, b"code")]))


def test_symlinks_and_duplicate_paths_fail():
    with pytest.raises(ValueError):
        read_snapshot(archive([("x.py", b"a"), ("x.py", b"b")]))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        tar.addfile(member)
    with pytest.raises(ValueError):
        read_snapshot(buffer.getvalue())


def test_repository_reads_every_query_page():
    dynamodb = MagicMock()
    dynamodb.query.side_effect = [
        {
            "Items": [{"file_key": {"S": "a.tar.gz"}}],
            "LastEvaluatedKey": {"file_id": {"S": "next"}},
        },
        {"Items": [{"file_key": {"S": "b.tar.gz"}}]},
    ]
    s3 = MagicMock()
    s3.get_object.side_effect = [
        {"Body": io.BytesIO(archive([("a.py", b"a")]))},
        {"Body": io.BytesIO(archive([("b.py", b"b")]))},
    ]
    files = CliSnapshotRepository(s3, dynamodb, "bucket", "table").get_files("batch")
    assert [f["path"] for f in files] == ["a.py", "b.py"]
    assert dynamodb.query.call_args.kwargs["ExclusiveStartKey"] == {
        "file_id": {"S": "next"}
    }


@pytest.mark.asyncio
async def test_cli_workflow_never_contacts_git():
    repository = MagicMock()
    repository.get_files.return_value = [
        {"path": "src/app.py", "content": "print('hello')"}
    ]
    mcp = MagicMock()
    model = MagicMock()

    async def answer(messages):
        return SimpleNamespace(content='{"issues":[]}')

    model.ainvoke = answer
    graph = LangGraphWorkflowBuilder(
        mcp, model, retrieval_node=CliRetrievalNode(repository)
    ).build()
    result = await graph.ainvoke(
        {
            "task_id": "test",
            "repository_url": "local://test",
            "branch": "working-tree",
            "commit_hash": "snapshot",
            "extra_args": {"batch_id": "batch"},
            "files": [],
            "scaned_files": 0,
            "issues": [],
            "expert_errors": [],
        }
    )
    assert result["final_output"]["scaned_files"] == 1
    assert result["final_output"]["coverage"]["complete"]
    mcp.get_tools.assert_not_called()


@pytest.mark.asyncio
async def test_large_scan_keeps_file_content_and_surfaces_failed_batch(monkeypatch):
    monkeypatch.setenv("TITVO_EXPERT_BATCH_BUDGET_CHARS", "22000")
    monkeypatch.setenv("TITVO_EXPERT_FILE_CAP_CHARS", "12000")
    sent = []

    async def answer(messages):
        sent.append(messages[-1].content)
        if len(sent) == 2:
            return SimpleNamespace(content="invalid JSON")
        return SimpleNamespace(content='{"issues":[]}')

    model = SimpleNamespace(ainvoke=answer)
    node = CodeVulnerabilitiesNode(model)
    files = [{"path": f"{i}.py", "content": "x" * 12000} for i in range(4)]
    output = await node({"files": files, "issues": []})
    assert len(sent) == 4
    assert all("x" * 12000 in content for content in sent)
    assert output["expert_metadata"]["code_vulnerabilities"]["batches_completed"] == 3
    result = MergeFindingsNode()(dict(output, scaned_files=4))["final_output"]
    assert result["status"] in {"FAILED", "WARNING"}
    assert not result["coverage"]["complete"]


def test_consolidation_retains_unrepresented_findings():
    """A valid model response cannot silently discard unrelated input findings."""
    from code_analysis.domain.entities.expert_result import ExpertIssue

    def issue(line, code):
        return ExpertIssue(
            title=code,
            description=code,
            severity="HIGH",
            category="same-category",
            path="same.py",
            line=line,
            summary=code,
            code=code,
            recommendation="fix",
        )

    original = [issue(1, "foo()"), issue(1, "bar()")]
    model = MagicMock()
    model.invoke.return_value = SimpleNamespace(
        content=json.dumps({"issues": [{**original[0].to_dict(), "source_ids": [0]}]})
    )
    result = MergeFindingsNode(model)({"issues": original, "scaned_files": 1})[
        "final_output"
    ]
    assert sorted(item["code"] for item in result["issues"]) == ["bar()", "foo()"]


def test_consolidation_rejects_mixed_evidence_tuple():
    """A line from one input cannot be paired with another input's snippet."""
    from code_analysis.domain.entities.expert_result import ExpertIssue

    first = ExpertIssue(
        title="first",
        description="first",
        severity="HIGH",
        category="a",
        path="same.py",
        line=1,
        summary="a",
        code="foo()",
        recommendation="fix",
    )
    second = ExpertIssue(
        title="second",
        description="second",
        severity="HIGH",
        category="a",
        path="same.py",
        line=2,
        summary="a",
        code="bar()",
        recommendation="fix",
    )
    model = MagicMock()
    model.invoke.return_value = SimpleNamespace(
        content=json.dumps(
            {"issues": [{**first.to_dict(), "line": 2, "source_ids": [0, 1]}]}
        )
    )
    result = MergeFindingsNode(model)({"issues": [first, second], "scaned_files": 1})[
        "final_output"
    ]
    assert sorted((item["line"], item["code"]) for item in result["issues"]) == [
        (1, "foo()"),
        (2, "bar()"),
    ]

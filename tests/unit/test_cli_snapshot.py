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


@pytest.mark.parametrize("with_manifest", [False, True])
def test_mixed_snapshot_excludes_binary_and_non_utf8(caplog, with_manifest):
    """Unexpected uploads do not discard valid Unicode source or alter its lines."""
    text = "# español\r\nprint('こんにちは')\n"
    entries = [
        ("src/app.py", text.encode()),
        ("image.png", b"\x89PNG\r\n\x1a\n"),
        ("binary.txt", b"secret\x00payload"),
        ("latin1.py", b"# caf\xe9"),
        ("empty.py", b""),
    ]
    if with_manifest:
        manifest = {
            "files": [
                {
                    "path": path,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for path, data in entries
            ]
        }
        entries.append((".titvo-manifest.json", json.dumps(manifest).encode()))
    assert read_snapshot(archive(entries)) == [
        {"path": "empty.py", "content": ""},
        {"path": "src/app.py", "content": text},
    ]
    assert "image.png" in caplog.text
    assert "latin1.py" in caplog.text
    assert "binary.txt" in caplog.text
    assert "NUL byte" in caplog.text
    assert "not valid UTF-8" in caplog.text
    assert "secret" not in caplog.text


def test_excluded_binary_still_requires_valid_manifest():
    """Filtering must never hide corruption in excluded content."""
    manifest = {"files": [{"path": "image.png", "size": 4, "sha256": "wrong"}]}
    with pytest.raises(ValueError, match="Snapshot integrity mismatch: image.png"):
        read_snapshot(
            archive(
                [
                    ("image.png", b"\x89PNG"),
                    (".titvo-manifest.json", json.dumps(manifest).encode()),
                ]
            )
        )


def snapshot_repository(packages):
    """Build the actual retrieval adapter with in-memory AWS responses."""
    dynamodb = MagicMock()
    dynamodb.query.return_value = {
        "Items": [{"file_key": {"S": f"{i}.tar.gz"}} for i in range(len(packages))]
    }
    s3 = MagicMock()
    s3.get_object.side_effect = [
        {"Body": io.BytesIO(archive(entries))} for entries in packages
    ]
    return CliSnapshotRepository(s3, dynamodb, "bucket", "table")


def test_binary_only_package_does_not_abort_other_packages():
    """The batch, rather than each package, must contain analyzable files."""
    repository = snapshot_repository(
        [[("image.png", b"\x89PNG")], [("app.py", b"print(1)")]]
    )
    assert repository.get_files("batch") == [{"path": "app.py", "content": "print(1)"}]


@pytest.mark.parametrize("second", [b"\x89PNG", b"valid text"])
def test_cross_package_duplicate_excluded_path_fails(second):
    """An excluded path cannot bypass cross-package duplicate validation."""
    repository = snapshot_repository(
        [[("same", b"\x89PNG")], [("same", second), ("app.py", b"print(1)")]]
    )
    with pytest.raises(ValueError, match="Duplicate CLI file: same"):
        repository.get_files("batch")


@pytest.mark.asyncio
async def test_binary_only_batch_is_explicit_retrieval_failure():
    """A batch with no text never becomes a successful zero-file scan."""
    repository = snapshot_repository(
        [[("image.png", b"\x89PNG")], [("data.bin", b"\x00abc")]]
    )
    result = await CliRetrievalNode(repository)({"extra_args": {"batch_id": "batch"}})
    assert result["files"] == []
    assert result["scaned_files"] == 0
    assert "no analyzable UTF-8 text files" in result["mcp_error"]


def test_empty_snapshot_still_fails():
    """An actually empty package remains a malformed snapshot."""
    with pytest.raises(ValueError, match="CLI snapshot contains no files"):
        read_snapshot(archive([]))


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

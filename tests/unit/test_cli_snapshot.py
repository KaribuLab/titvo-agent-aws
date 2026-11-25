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


def snapshot_repository(packages, *, keys=None, bodies=None):
    """Build the actual retrieval adapter with in-memory AWS responses."""
    dynamodb = MagicMock()
    object_keys = keys or [f"{i}.tar.gz" for i in range(len(packages))]
    dynamodb.query.return_value = {
        "Items": [{"file_key": {"S": key}} for key in object_keys]
    }
    s3 = MagicMock()
    if bodies is not None:
        payloads = bodies
    else:
        payloads = [archive(entries) for entries in packages]
    s3.get_object.side_effect = [{"Body": io.BytesIO(payload)} for payload in payloads]
    return CliSnapshotRepository(s3, dynamodb, "bucket", "table")


def _scan_manifest(**overrides):
    base = {
        "version": 1,
        "scope": "full",
        "commit": "abc",
        "branch": "main",
        "focus_files": [],
        "files": [],
    }
    base.update(overrides)
    return base


def test_mixed_batch_parses_scan_manifest_without_opening_as_tar():
    """Sidecar JSON must never be fed to tarfile; tar still yields analyzable files."""
    data = b"print(1)"
    tar_bytes = archive([("app.py", data)])
    manifest = _scan_manifest(
        scope="commit",
        focus_files=["app.py"],
        files=[
            {
                "path": "app.py",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    )
    repository = snapshot_repository(
        [],
        keys=[
            "temp/batch/scan-manifest.json",
            "temp/batch/snapshot.tar.gz",
        ],
        bodies=[json.dumps(manifest).encode(), tar_bytes],
    )
    batch = repository.get_batch("batch")
    assert batch.scan_manifest == manifest
    assert batch.files == [{"path": "app.py", "content": "print(1)"}]


def test_focus_files_filters_analyzable_paths():
    data_a = b"a=1"
    data_b = b"b=2"
    tar_bytes = archive([("a.py", data_a), ("b.py", data_b)])
    manifest = _scan_manifest(
        scope="staged",
        focus_files=["a.py"],
        files=[
            {
                "path": "a.py",
                "size": len(data_a),
                "sha256": hashlib.sha256(data_a).hexdigest(),
            },
            {
                "path": "b.py",
                "size": len(data_b),
                "sha256": hashlib.sha256(data_b).hexdigest(),
            },
        ],
    )
    repository = snapshot_repository(
        [],
        keys=["temp/b/scan-manifest.json", "temp/b/snapshot.tar.gz"],
        bodies=[json.dumps(manifest).encode(), tar_bytes],
    )
    assert repository.get_files("batch") == [{"path": "a.py", "content": "a=1"}]


def test_empty_focus_returns_all_analyzable():
    tar_bytes = archive([("a.py", b"a"), ("b.py", b"b")])
    manifest = _scan_manifest(focus_files=[])
    repository = snapshot_repository(
        [],
        keys=["scan-manifest.json", "snapshot.tar.gz"],
        bodies=[json.dumps(manifest).encode(), tar_bytes],
    )
    assert [f["path"] for f in repository.get_files("batch")] == ["a.py", "b.py"]


def test_deleted_focus_path_tolerates_missing_content():
    tar_bytes = archive([("kept.py", b"ok")])
    manifest = _scan_manifest(
        scope="commit",
        focus_files=["deleted.py", "kept.py"],
        files=[
            {
                "path": "kept.py",
                "size": 2,
                "sha256": hashlib.sha256(b"ok").hexdigest(),
            }
        ],
    )
    repository = snapshot_repository(
        [],
        keys=["scan-manifest.json", "x.tar.gz"],
        bodies=[json.dumps(manifest).encode(), tar_bytes],
    )
    assert repository.get_files("batch") == [
        {"path": "deleted.py", "content": ""},
        {"path": "kept.py", "content": "ok"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", [[], ["src/app.py", "nested/.titvoignore.yml"]])
async def test_root_policy_is_separate_even_when_excluded_from_focus(focus):
    """Root transport metadata survives focus; nested namesakes remain source."""
    policy = "version: 1\nexceptions: []\n"
    entries = [
        (".titvoignore.yml", policy.encode()),
        ("nested/.titvoignore.yml", b"ordinary nested source"),
        ("src/app.py", b"print(1)"),
        ("src/background.py", b"background = True"),
    ]
    manifest = _scan_manifest(
        scope="commit" if focus else "full",
        focus_files=focus,
        files=[
            {
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in entries
        ],
    )
    # Exercise both transport integrity and sidecar focus through the public node.
    integrity_manifest = {"version": 1, "files": manifest["files"]}
    repository = snapshot_repository(
        [],
        keys=["scan-manifest.json", "snapshot.tar.gz"],
        bodies=[
            json.dumps(manifest).encode(),
            archive(entries + [
                (".titvo-manifest.json", json.dumps(integrity_manifest).encode())
            ]),
        ],
    )
    result = await CliRetrievalNode(repository)({"extra_args": {"batch_id": "batch"}})
    expected_files = [
        {"path": path, "content": data.decode()}
        for path, data in sorted(entries)
        if path != ".titvoignore.yml" and (not focus or path in focus)
    ]
    if focus:
        expected_files.sort(key=lambda item: focus.index(item["path"]))
    assert result == {
        "accepted_exceptions_text": policy,
        "files": expected_files,
        "scaned_files": len(expected_files),
        "mcp_error": None,
        "rag_chunks": [],
    }


def test_unknown_batch_object_type_fails():
    repository = snapshot_repository(
        [],
        keys=["temp/batch/notes.txt"],
        bodies=[b"not a tar"],
    )
    with pytest.raises(ValueError, match="Unsupported CLI batch object"):
        repository.get_files("batch")


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
    from code_analysis.infra.adapters.cli_snapshot import CliBatchSnapshot

    repository = MagicMock()
    repository.get_batch.return_value = CliBatchSnapshot(
        files=[{"path": "src/app.py", "content": "print('hello')"}],
        scan_manifest=None,
    )
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

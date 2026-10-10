"""Tests for two-level consolidation, accountability and incomplete scans."""

import json
from unittest.mock import MagicMock

from code_analysis.domain.entities.expert_result import ExpertIssue
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)


def _issue(
    title: str,
    path: str = "src/a.ts",
    line: int = 10,
    severity: str = "MEDIUM",
    category: str = "Cat",
    code: str = "foo();",
    expert: str = "owasp_web",
    batch_index: int = 0,
) -> ExpertIssue:
    return ExpertIssue(
        title=title,
        description=title,
        severity=severity,
        category=category,
        path=path,
        line=line,
        summary=title,
        code=code,
        recommendation="fix",
        metadata={"expert": expert, "batch_index": batch_index, "chunk_index": 0},
    )


def _state(issues, **extra):
    return {
        "task_id": "t",
        "repository_url": "",
        "commit_hash": "",
        "extra_args": {},
        "files": [],
        "scaned_files": 5,
        "issues": issues,
        **extra,
    }


def _consolidated(items: list[dict]) -> MagicMock:
    return MagicMock(content=json.dumps({"issues": items}))


def _out(issue: ExpertIssue, source_ids: list[int], **override) -> dict:
    return {
        "source_ids": source_ids,
        "title": override.get("title", issue.title),
        "description": issue.description,
        "severity": override.get("severity", issue.severity),
        "category": override.get("category", issue.category),
        "path": override.get("path", issue.path),
        "line": override.get("line", issue.line),
        "summary": issue.summary,
        "code": override.get("code", issue.code),
        "recommendation": issue.recommendation,
    }


class TestOrderingAndL1:
    def test_issues_are_sorted_deterministically_before_merge(self):
        node = MergeFindingsNode()
        a = _issue("B", path="z.py", expert="owasp_web", batch_index=1)
        b = _issue("A", path="a.py", expert="owasp_api", batch_index=0)
        out1 = node(_state([a, b]))["final_output"]["issues"]
        out2 = node(_state([b, a]))["final_output"]["issues"]
        assert out1 == out2
        assert [i["title"] for i in out1] == ["A", "B"]

    def test_l1_merges_identical_evidence_only(self):
        node = MergeFindingsNode()
        same1 = _issue("X", expert="owasp_web", severity="MEDIUM")
        same2 = _issue("X dup", expert="owasp_api", severity="HIGH")
        other_code = _issue("Y", code="bar();", expert="owasp_api")
        result = node(_state([same1, same2, other_code]))
        issues = result["final_output"]["issues"]
        assert len(issues) == 2
        merged = issues[0]
        assert merged["severity"] == "HIGH"
        assert merged["merged_from"] == ["owasp_api", "owasp_web"]
        assert result["expert_metadata"]["consolidation"]["l1_in"] == 3
        assert result["expert_metadata"]["consolidation"]["l1_out"] == 2

    def test_merge_does_not_return_issues_key(self):
        result = MergeFindingsNode()(_state([_issue("A")]))
        assert "issues" not in result
        assert result["final_output"]["issues"][0]["title"] == "A"


class TestL2Accountability:
    def test_single_finding_groups_skip_the_model(self):
        model = MagicMock()
        node = MergeFindingsNode(model)
        node(_state([_issue("A", path="a.ts"), _issue("B", path="b.ts")]))
        model.invoke.assert_not_called()

    def test_groups_are_per_file_and_bounded(self):
        model = MagicMock()
        model.invoke.side_effect = lambda messages: _consolidated(
            [
                _out(
                    _issue("x", path=p, line=ln, code=c),
                    [i],
                    title=f"keep {i}",
                )
                for i, (p, ln, c) in enumerate(_parse_findings(messages[0].content))
            ]
        )
        node = MergeFindingsNode(model)
        issues = []
        for f in range(40):
            for k in range(3):
                issues.append(
                    _issue(
                        f"f{f}-{k}", path=f"src/f{f}.ts", line=k + 1, code=f"c{k}();"
                    )
                )
        result = node(_state(issues))
        assert model.invoke.call_count == 40
        for call in model.invoke.call_args_list:
            findings = _parse_findings(call.args[0][0].content)
            assert len(findings) == 3
            assert len({p for p, _, _ in findings}) == 1
        assert len(result["final_output"]["issues"]) == 120

    def test_more_than_20_findings_in_a_file_are_chunked(self):
        model = MagicMock()
        model.invoke.side_effect = lambda messages: _consolidated(
            [
                _out(_issue("x", line=ln, code=c), [i])
                for i, (_, ln, c) in enumerate(_parse_findings(messages[0].content))
            ]
        )
        node = MergeFindingsNode(model)
        issues = [_issue(f"i{n}", line=n + 1, code=f"c{n}();") for n in range(45)]
        node(_state(issues))
        sizes = [
            len(_parse_findings(c.args[0][0].content))
            for c in model.invoke.call_args_list
        ]
        assert sizes == [20, 20, 5]

    def test_valid_consolidation_is_accepted(self):
        # Sorted order inside merge is (expert, batch, path, line): a, b, c.
        a = _issue("A", line=10, severity="MEDIUM", expert="owasp_api")
        b = _issue("B", line=10, severity="HIGH", code="bar();", expert="owasp_web")
        c = _issue("C", line=30, code="baz();", expert="owasp_web")
        model = MagicMock()
        model.invoke.return_value = _consolidated(
            [
                _out(a, [0, 1], title="A+B", severity="HIGH"),
                _out(c, [2]),
            ]
        )
        result = MergeFindingsNode(model)(_state([a, b, c]))
        issues = result["final_output"]["issues"]
        assert [i["title"] for i in issues] == ["A+B", "C"]
        assert issues[0]["severity"] == "HIGH"
        assert issues[0]["merged_from"] == ["owasp_api", "owasp_web"]
        assert "source_ids" not in issues[0]
        metrics = result["expert_metadata"]["consolidation"]
        assert metrics == {
            "l1_in": 3,
            "l1_out": 3,
            "l2_in": 3,
            "l2_out": 2,
            "l2_groups": 1,
            "l2_groups_rejected": 0,
        }

    def test_consolidation_that_loses_a_finding_is_rejected(self):
        a = _issue("A", line=10)
        b = _issue("B", line=20, code="bar();")
        c = _issue("C", line=30, code="baz();")
        model = MagicMock()
        model.invoke.return_value = _consolidated([_out(a, [0]), _out(b, [1])])
        result = MergeFindingsNode(model)(_state([a, b, c]))
        issues = result["final_output"]["issues"]
        assert [i["title"] for i in issues] == ["A", "B", "C"]
        assert result["expert_metadata"]["consolidation"]["l2_groups_rejected"] == 1

    def test_duplicated_source_id_is_rejected(self):
        a = _issue("A", line=10)
        b = _issue("B", line=20, code="bar();")
        model = MagicMock()
        model.invoke.return_value = _consolidated([_out(a, [0, 1]), _out(b, [1])])
        result = MergeFindingsNode(model)(_state([a, b]))
        assert len(result["final_output"]["issues"]) == 2

    def test_missing_source_ids_is_rejected(self):
        a = _issue("A", line=10)
        b = _issue("B", line=20, code="bar();")
        model = MagicMock()
        out = _out(a, [0, 1])
        del out["source_ids"]
        model.invoke.return_value = _consolidated([out])
        result = MergeFindingsNode(model)(_state([a, b]))
        assert len(result["final_output"]["issues"]) == 2

    def test_lowered_severity_is_rejected(self):
        a = _issue("A", line=10, severity="HIGH")
        b = _issue("B", line=20, code="bar();", severity="LOW")
        model = MagicMock()
        model.invoke.return_value = _consolidated([_out(a, [0, 1], severity="LOW")])
        result = MergeFindingsNode(model)(_state([a, b]))
        assert len(result["final_output"]["issues"]) == 2

    def test_invented_code_or_line_is_rejected(self):
        a = _issue("A", line=10)
        b = _issue("B", line=20, code="bar();")
        model = MagicMock()
        model.invoke.return_value = _consolidated([_out(a, [0, 1], code="evil();")])
        assert (
            len(MergeFindingsNode(model)(_state([a, b]))["final_output"]["issues"]) == 2
        )
        model.invoke.return_value = _consolidated([_out(a, [0, 1], line=99)])
        assert (
            len(MergeFindingsNode(model)(_state([a, b]))["final_output"]["issues"]) == 2
        )

    def test_failure_in_one_group_does_not_affect_others(self):
        a1 = _issue("A1", path="a.ts", line=1)
        a2 = _issue("A2", path="a.ts", line=2, code="bar();")
        b1 = _issue("B1", path="b.ts", line=1)
        b2 = _issue("B2", path="b.ts", line=2, code="bar();")
        model = MagicMock()
        model.invoke.side_effect = [
            MagicMock(content="not json"),
            MagicMock(content="still not json"),  # repair attempt for a.ts
            _consolidated([_out(b1, [0, 1], title="B merged")]),
        ]
        result = MergeFindingsNode(model)(_state([a1, a2, b1, b2]))
        titles = [i["title"] for i in result["final_output"]["issues"]]
        assert titles == ["A1", "A2", "B merged"]
        assert result["expert_metadata"]["consolidation"]["l2_groups_rejected"] == 1

    def test_same_secret_in_two_files_stays_two_issues(self):
        a = _issue("Secret", path="a.ts", code="const k = 'x'")
        b = _issue("Secret", path="b.ts", code="const k = 'x'")
        model = MagicMock()
        result = MergeFindingsNode(model)(_state([a, b]))
        assert len(result["final_output"]["issues"]) == 2
        model.invoke.assert_not_called()


class TestIncomplete:
    def _failed(self, expert="owasp_api", idx=1, paths=("src/a.ts", "src/b.ts")):
        return {
            "expert": expert,
            "batch_index": idx,
            "paths": list(paths),
            "error": "boom",
        }

    def test_incomplete_without_issues_is_warning(self):
        result = MergeFindingsNode()(
            _state(
                [],
                failed_batches=[self._failed()],
                expert_metadata={
                    "owasp_api": {"batches": 3, "failed_batches": 1},
                    "owasp_web": {"batches": 2, "failed_batches": 0},
                },
            )
        )
        out = result["final_output"]
        assert out["status"] == "WARNING"
        assert out["incomplete"]["files_not_fully_analyzed"] == ["src/a.ts", "src/b.ts"]
        assert out["incomplete"]["message"] == (
            "Análisis incompleto: 1 de 5 lotes fallaron; "
            "2 archivos sin analizar completamente"
        )
        assert out["incomplete"]["failed_batches"][0]["expert"] == "owasp_api"

    def test_incomplete_with_high_issue_stays_failed(self):
        result = MergeFindingsNode()(
            _state([_issue("A", severity="HIGH")], failed_batches=[self._failed()])
        )
        assert result["final_output"]["status"] == "FAILED"
        assert "incomplete" in result["final_output"]

    def test_complete_scan_has_no_incomplete_key(self):
        result = MergeFindingsNode()(_state([], failed_batches=[]))
        assert result["final_output"]["status"] == "COMPLETED"
        assert "incomplete" not in result["final_output"]

    def test_failed_batches_are_sorted(self):
        result = MergeFindingsNode()(
            _state(
                [],
                failed_batches=[
                    self._failed("owasp_web", 2, ("z.ts",)),
                    self._failed("owasp_api", 0, ("a.ts",)),
                ],
            )
        )
        fbs = result["final_output"]["incomplete"]["failed_batches"]
        assert [(f["expert"], f["batch_index"]) for f in fbs] == [
            ("owasp_api", 0),
            ("owasp_web", 2),
        ]


def _parse_findings(prompt: str) -> list[tuple[str, int, str]]:
    start = prompt.index("Hallazgos de entrada:") + len("Hallazgos de entrada:")
    data = json.loads(prompt[start:].strip())
    return [(d["path"], d["line"], d["code"]) for d in data]


class TestProviderUnavailable:
    def test_provider_error_is_failed_with_explicit_error(self):
        node = MergeFindingsNode(model=None)
        state = _state(
            [],
            failed_batches=[
                {
                    "expert": "owasp_web",
                    "batch_index": 0,
                    "paths": ["src/a.ts"],
                    "error": "aborted: 429 insufficient_quota: no credits",
                }
            ],
            expert_metadata={"owasp_web": {"batches": 1, "failed_batches": 1}},
            provider_error="429 insufficient_quota: You have no credits remaining",
        )

        result = node(state)
        output = result["final_output"]

        assert output["status"] == "FAILED"
        assert output["error"] == (
            "Proveedor LLM no disponible: 429 insufficient_quota: "
            "You have no credits remaining"
        )
        assert output["incomplete"]["files_not_fully_analyzed"] == ["src/a.ts"]

    def test_provider_error_skips_l2_and_keeps_l1(self):
        model = MagicMock()
        issues = [
            _issue("A", line=10, expert="owasp_web"),
            _issue("B", line=20, expert="owasp_api"),
        ]
        node = MergeFindingsNode(model=model)

        result = node(_state(issues, provider_error="401 invalid_api_key: bad key"))

        model.invoke.assert_not_called()
        output = result["final_output"]
        assert output["status"] == "FAILED"
        assert {i["title"] for i in output["issues"]} == {"A", "B"}
        assert (
            result["expert_metadata"]["consolidation"]["l2_skipped_reason"]
            == "provider_unavailable"
        )

    def test_without_provider_error_incomplete_scan_stays_warning(self):
        node = MergeFindingsNode(model=None)
        state = _state(
            [],
            failed_batches=[
                {"expert": "owasp_web", "batch_index": 0, "paths": ["x"], "error": "e"}
            ],
            expert_metadata={"owasp_web": {"batches": 2, "failed_batches": 1}},
            provider_error=None,
        )
        output = node(state)["final_output"]
        assert output["status"] == "WARNING"
        assert "error" not in output

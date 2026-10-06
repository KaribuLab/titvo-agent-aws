"""Tests for FindingsMerger level-1 (exact evidence) deduplication."""

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult
from code_analysis.domain.services.findings_merger import FindingsMerger


def _issue(
    title: str,
    severity: str = "MEDIUM",
    path: str = "src/app.py",
    line: int = 1,
    code: str = "example();",
) -> ExpertIssue:
    return ExpertIssue(
        title=title,
        description=title,
        severity=severity,
        category="Security",
        path=path,
        line=line,
        summary=title,
        code=code,
        recommendation="Fix",
    )


class TestFindingsMerger:
    """Tests for deterministic L1 dedupe."""

    def test_empty_merge(self):
        """Empty collection should return COMPLETED."""
        merger = FindingsMerger()
        result = merger.to_dict(scaned_files=0)

        assert result["status"] == "COMPLETED"
        assert result["issues"] == []

    def test_single_expert_result(self):
        """Single expert result should be preserved."""
        merger = FindingsMerger()
        issue = _issue("SQL Injection", severity="CRITICAL", path="src/db.py")

        merger.add_expert_result(ExpertResult("code_vulnerabilities", [issue]))

        collected = merger.get_merged_issues()
        assert len(collected) == 1
        assert collected[0].title == "SQL Injection"

    def test_identical_evidence_is_merged_with_highest_severity(self):
        """Same (path, line, category, code) from two experts → one issue."""
        merger = FindingsMerger()
        issue1 = _issue("Token storage", severity="MEDIUM")
        issue2 = _issue("Token storage duplicate", severity="HIGH")

        merger.add_expert_result(ExpertResult("web", [issue1]))
        merger.add_expert_result(ExpertResult("mobile", [issue2]))

        collected = merger.get_merged_issues()
        assert len(collected) == 1
        assert collected[0].title == issue1.title  # first seen wins the text
        assert collected[0].severity == "HIGH"
        assert collected[0].metadata["merged_from"] == ["mobile", "web"]

    def test_same_location_different_code_is_kept(self):
        """Different evidence at the same location is NOT a duplicate."""
        merger = FindingsMerger()
        merger.add_expert_result(ExpertResult("web", [_issue("A", code="a();")]))
        merger.add_expert_result(ExpertResult("api", [_issue("B", code="b();")]))
        assert len(merger.get_merged_issues()) == 2

    def test_same_location_different_category_is_kept(self):
        merger = FindingsMerger()
        a = _issue("A")
        b = _issue("B")
        b.category = "Other"
        merger.add_expert_result(ExpertResult("web", [a]))
        merger.add_expert_result(ExpertResult("api", [b]))
        assert len(merger.get_merged_issues()) == 2

    def test_whitespace_differences_in_code_still_merge(self):
        merger = FindingsMerger()
        merger.add_expert_result(ExpertResult("web", [_issue("A", code="foo( x );")]))
        merger.add_expert_result(ExpertResult("api", [_issue("B", code="foo(x);")]))
        assert len(merger.get_merged_issues()) == 2  # normalization is whitespace only
        merger2 = FindingsMerger()
        merger2.add_expert_result(ExpertResult("web", [_issue("A", code="foo(x);  ")]))
        merger2.add_expert_result(ExpertResult("api", [_issue("B", code="foo(x);")]))
        assert len(merger2.get_merged_issues()) == 1

    def test_dedupe_static_uses_issue_metadata_expert(self):
        a = _issue("A")
        a.metadata = {"expert": "owasp_web", "batch_index": 0}
        b = _issue("B")
        b.metadata = {"expert": "owasp_api", "batch_index": 2}
        merged = FindingsMerger.dedupe([a, b])
        assert len(merged) == 1
        assert merged[0].metadata["merged_from"] == ["owasp_api", "owasp_web"]
        assert merged[0].metadata["expert"] == "owasp_web"

    def test_dedupe_does_not_mutate_inputs(self):
        a = _issue("A", severity="LOW")
        b = _issue("B", severity="HIGH")
        FindingsMerger.dedupe([a, b])
        assert a.severity == "LOW"
        assert "merged_from" not in a.metadata

    def test_ignores_error_results(self):
        """Failed expert results should be skipped."""
        merger = FindingsMerger()
        issue = _issue("Valid finding")

        merger.add_expert_result(
            ExpertResult("failed", [_issue("Skipped")], error="boom")
        )
        merger.add_expert_result(ExpertResult("ok", [issue]))

        collected = merger.get_merged_issues()
        assert len(collected) == 1
        assert collected[0].title == "Valid finding"

    def test_status_failed_with_high_or_critical(self):
        """Status should be FAILED with HIGH or CRITICAL issues."""
        merger = FindingsMerger()
        merger.add_expert_result(ExpertResult("expert", [_issue("High", "HIGH")]))

        assert merger.get_final_status() == "FAILED"

    def test_status_warning_with_medium_or_low(self):
        """Status should be WARNING with only MEDIUM/LOW issues."""
        merger = FindingsMerger()
        merger.add_expert_result(ExpertResult("expert", [_issue("Medium", "MEDIUM")]))

        assert merger.get_final_status() == "WARNING"

    def test_to_dict_preserves_all_issues(self):
        """to_dict should serialize every collected issue."""
        merger = FindingsMerger()
        merger.add_expert_result(
            ExpertResult("expert", [_issue("One"), _issue("Two", path="src/two.py")])
        )

        result = merger.to_dict(scaned_files=2)

        assert result["status"] == "WARNING"
        assert result["scaned_files"] == 2
        assert [issue["title"] for issue in result["issues"]] == ["One", "Two"]

    def test_merge_results_collects_distinct_results(self):
        """merge_results keeps every distinct-evidence issue."""
        result = FindingsMerger.merge_results(
            [
                ExpertResult("web", [_issue("One")]),
                ExpertResult("mobile", [_issue("Two", path="src/two.py")]),
            ],
            scaned_files=3,
        )

        assert result["scaned_files"] == 3
        assert [issue["title"] for issue in result["issues"]] == ["One", "Two"]

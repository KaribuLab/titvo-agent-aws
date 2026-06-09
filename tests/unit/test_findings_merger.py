"""Tests for FindingsMerger collection behavior."""

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
    """Tests for non-deterministic findings collection."""

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

    def test_preserves_duplicates_for_agent_consolidation(self):
        """Duplicate-looking issues should not be deduplicated in code."""
        merger = FindingsMerger()
        issue1 = _issue("Token storage", severity="HIGH")
        issue2 = _issue("Token storage duplicate", severity="HIGH")

        merger.add_expert_result(ExpertResult("web", [issue1]))
        merger.add_expert_result(ExpertResult("mobile", [issue2]))

        collected = merger.get_merged_issues()
        assert len(collected) == 2
        assert collected[0].title == issue1.title
        assert collected[1].title == issue2.title

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

    def test_merge_results_collects_all_results(self):
        """merge_results should collect every issue from all expert results."""
        result = FindingsMerger.merge_results(
            [
                ExpertResult("web", [_issue("One")]),
                ExpertResult("mobile", [_issue("Two")]),
            ],
            scaned_files=3,
        )

        assert result["scaned_files"] == 3
        assert [issue["title"] for issue in result["issues"]] == ["One", "Two"]

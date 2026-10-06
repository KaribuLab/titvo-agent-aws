"""Level-1 (deterministic) consolidation of expert findings.

Two findings are duplicates only when they share ``(path, line, category,
normalized code)`` — identical evidence. The surviving entry keeps the highest
severity and records every contributing expert in ``metadata.merged_from``.
Everything that is *not* an exact duplicate is preserved for the level-2
consolidation, which is model-led and validated separately.
"""

import logging
from dataclasses import replace
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult

LOGGER = logging.getLogger(__name__)

SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def severity_rank(severity: str | None) -> int:
    return SEVERITY_ORDER.get(str(severity or "").upper(), 0)


def max_severity(severities: list[str]) -> str:
    if not severities:
        return "MEDIUM"
    return max(severities, key=severity_rank)


class FindingsMerger:
    """Collects findings and deduplicates them by exact evidence (L1)."""

    def __init__(self) -> None:
        self._issues: dict[tuple[str, int, str, str], ExpertIssue] = {}

    def add_issue(self, issue: ExpertIssue, expert_name: str | None = None) -> None:
        """Add one issue, merging it with an identical-evidence duplicate."""
        contributors = self._contributors(issue, expert_name)
        key = issue.get_dedup_key()
        existing = self._issues.get(key)
        if existing is None:
            metadata = {k: v for k, v in issue.metadata.items() if k != "merged_from"}
            metadata["merged_from"] = contributors
            self._issues[key] = replace(issue, metadata=metadata)
            return

        merged_from = sorted(
            set(existing.metadata.get("merged_from", [])) | set(contributors)
        )
        severity = max_severity([existing.severity, issue.severity])
        self._issues[key] = replace(
            existing,
            severity=severity,
            metadata={**existing.metadata, "merged_from": merged_from},
        )

    def add_expert_result(self, result: ExpertResult) -> None:
        """Add every issue of an expert result (failed results are skipped)."""
        if result.error:
            LOGGER.warning(
                "Expert %s failed with error: %s", result.expert_name, result.error
            )
            return
        LOGGER.info(
            "Processing %d issues from %s", len(result.issues), result.expert_name
        )
        for issue in result.issues:
            self.add_issue(issue, result.expert_name)

    def get_merged_issues(self) -> list[ExpertIssue]:
        """Return deduplicated issues in first-seen order."""
        return list(self._issues.values())

    @staticmethod
    def dedupe(issues: list[ExpertIssue]) -> list[ExpertIssue]:
        """One-shot L1 dedupe preserving first-seen order."""
        merger = FindingsMerger()
        for issue in issues:
            merger.add_issue(issue)
        return merger.get_merged_issues()

    def get_final_status(self) -> str:
        issues = self.get_merged_issues()
        if not issues:
            return "COMPLETED"
        if any(issue.severity in ("CRITICAL", "HIGH") for issue in issues):
            return "FAILED"
        return "WARNING"

    def to_dict(self, scaned_files: int) -> dict[str, Any]:
        issues = self.get_merged_issues()
        return {
            "status": self.get_final_status(),
            "scaned_files": scaned_files,
            "issues": [issue.to_dict() for issue in issues],
        }

    @staticmethod
    def merge_results(
        results: list[ExpertResult],
        scaned_files: int,
    ) -> dict[str, Any]:
        merger = FindingsMerger()
        for result in results:
            merger.add_expert_result(result)
        return merger.to_dict(scaned_files)

    @staticmethod
    def _contributors(issue: ExpertIssue, expert_name: str | None) -> list[str]:
        names: set[str] = set()
        merged_from = issue.metadata.get("merged_from")
        if isinstance(merged_from, list):
            names.update(str(n) for n in merged_from)
        expert = issue.metadata.get("expert") or expert_name
        if expert:
            names.add(str(expert))
        return sorted(names) if names else ["unknown"]

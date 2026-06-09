"""Service for collecting findings from multiple experts."""

import logging
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult

LOGGER = logging.getLogger(__name__)


class FindingsMerger:
    """Collects findings without deterministic deduplication.

    Consolidation is owned by the LangGraph consolidation agent. This service
    intentionally preserves every issue it receives.
    """

    def __init__(self) -> None:
        self._issues: list[ExpertIssue] = []

    def add_expert_result(self, result: ExpertResult) -> None:
        """Add issues from a single expert result without changing them."""
        if result.error:
            LOGGER.warning(
                "Expert %s failed with error: %s",
                result.expert_name,
                result.error,
            )
            return

        LOGGER.info(
            "Processing %d issues from %s",
            len(result.issues),
            result.expert_name,
        )

        self._issues.extend(result.issues)

    def get_merged_issues(self) -> list[ExpertIssue]:
        """Return all collected issues as a list.

        Kept for compatibility with existing callers; this method does not merge.
        """
        return list(self._issues)

    def get_final_status(self) -> str:
        """Determine final status based on merged issues.

        Returns:
            - FAILED: Any CRITICAL or HIGH issue
            - WARNING: Only MEDIUM or LOW issues
            - COMPLETED: No issues
        """
        issues = self.get_merged_issues()

        if not issues:
            return "COMPLETED"

        has_critical_or_high = any(
            issue.severity in ("CRITICAL", "HIGH") for issue in issues
        )

        if has_critical_or_high:
            return "FAILED"

        return "WARNING"

    def to_dict(self, scaned_files: int) -> dict[str, Any]:
        """Generate final output dictionary."""
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
        """Static method to merge multiple expert results.

        Convenience method for one-shot merging.
        """
        merger = FindingsMerger()

        for result in results:
            merger.add_expert_result(result)

        return merger.to_dict(scaned_files)

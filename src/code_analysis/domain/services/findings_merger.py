"""Service for merging and deduplicating findings from multiple experts."""

import logging
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult

LOGGER = logging.getLogger(__name__)


class FindingsMerger:
    """Merges findings from multiple security experts with deduplication.

    Deduplication key: evidence first, then legacy (path, line, category)
    Severity conflict resolution: Keep lower severity (conservative)
    """

    SEVERITY_ORDER = {
        "CRITICAL": 4,
        "HIGH": 3,
        "MEDIUM": 2,
        "LOW": 1,
    }

    def __init__(self) -> None:
        self._issues: dict[tuple[Any, ...], ExpertIssue] = {}

    def add_expert_result(self, result: ExpertResult) -> None:
        """Add issues from a single expert result.

        Deduplicates by concrete evidence when available.
        On conflict, keeps the lower severity (conservative approach).
        """
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

        for issue in result.issues:
            dedup_key = self._find_duplicate_key(issue)

            if dedup_key is None:
                # First occurrence
                dedup_key = issue.get_dedup_key()
                self._issues[dedup_key] = issue
                LOGGER.debug(
                    "New issue: %s at %s:%d",
                    issue.category,
                    issue.path,
                    issue.line,
                )
            else:
                # Duplicate found - apply conservative merge
                existing = self._issues[dedup_key]
                self._issues[dedup_key] = self._merge_duplicate(
                    existing,
                    issue,
                    result.expert_name,
                )

    def _find_duplicate_key(self, issue: ExpertIssue) -> tuple[Any, ...] | None:
        """Find an existing issue that represents the same evidence."""
        for key, existing in self._issues.items():
            if self._is_duplicate(existing, issue):
                return key
        return None

    def _is_duplicate(self, existing: ExpertIssue, issue: ExpertIssue) -> bool:
        """Return true when two issues should be represented once."""
        if self._normalize_path(existing.path) != self._normalize_path(issue.path):
            return False
        if existing.line != issue.line:
            return False

        existing_code = self._normalize_code(existing.code)
        issue_code = self._normalize_code(issue.code)

        if existing_code and issue_code:
            return existing_code == issue_code
        if existing_code or issue_code:
            return True

        return existing.category == issue.category

    def _merge_duplicate(
        self,
        existing: ExpertIssue,
        issue: ExpertIssue,
        expert_name: str,
    ) -> ExpertIssue:
        """Merge duplicates while keeping the current issue shape."""
        primary = self._choose_primary_issue(existing, issue)
        merged_severity = self._merge_severities(existing.severity, issue.severity)

        if merged_severity == primary.severity:
            return primary

        LOGGER.info(
            "Severity conflict for %s at %s:%d - using conservative value: %s",
            issue.category,
            issue.path,
            issue.line,
            merged_severity,
        )

        return ExpertIssue(
            title=primary.title,
            description=primary.description,
            severity=merged_severity,
            category=primary.category,
            path=primary.path,
            line=primary.line,
            summary=primary.summary,
            code=primary.code,
            recommendation=primary.recommendation,
            metadata={
                **primary.metadata,
                "merged_from": expert_name,
                "original_severity": existing.severity,
                "conflicting_severity": issue.severity,
            },
        )

    def _choose_primary_issue(
        self,
        existing: ExpertIssue,
        issue: ExpertIssue,
    ) -> ExpertIssue:
        """Prefer the issue with concrete code evidence."""
        existing_has_code = bool(self._normalize_code(existing.code))
        issue_has_code = bool(self._normalize_code(issue.code))

        if issue_has_code and not existing_has_code:
            return issue
        return existing

    @staticmethod
    def _normalize_path(path: str) -> str:
        return path.strip()

    @staticmethod
    def _normalize_code(code: str) -> str:
        return " ".join((code or "").split())

    def _merge_severities(self, sev1: str, sev2: str) -> str:
        """Return the more conservative (lower) severity."""
        order1 = self.SEVERITY_ORDER.get(sev1, 0)
        order2 = self.SEVERITY_ORDER.get(sev2, 0)

        # Return the lower severity value
        if order1 <= order2:
            return sev1
        return sev2

    def get_merged_issues(self) -> list[ExpertIssue]:
        """Return all merged issues as a list."""
        return list(self._issues.values())

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

"""Merge Findings Node for LangGraph workflow.

Final node that deduplicates and determines final status.
"""

import json
import logging
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult
from code_analysis.domain.services.findings_merger import FindingsMerger
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)


class MergeFindingsNode:
    """Node for merging expert findings and determining final status."""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def __call__(self, state: AgentState) -> dict[str, Any]:
        """Merge findings and return final result.

        Args:
            state: Current workflow state with all issues

        Returns:
            Final state with status and formatted output
        """
        try:
            issues = state.get("issues", [])
            scaned_files = state.get("scaned_files", 0)
            expert_errors = state.get("expert_errors", [])

            LOGGER.info(
                "Merging %d issues from experts (%d expert errors)",
                len(issues),
                len(expert_errors),
            )

            # Log any expert errors
            if expert_errors:
                for error in expert_errors:
                    LOGGER.warning("Expert error: %s", error)

            # Create merger and process all issues
            merger = FindingsMerger()
            merger.add_expert_result(ExpertResult(expert_name="merged", issues=issues))
            unique_issues = self._semantic_deduplicate(merger.get_merged_issues())

            LOGGER.info("After deduplication: %d unique issues", len(unique_issues))

            # Determine status
            has_critical_or_high = any(
                issue.severity in ("CRITICAL", "HIGH") for issue in unique_issues
            )

            if has_critical_or_high:
                status = "FAILED"
            elif unique_issues:
                status = "WARNING"
            else:
                status = "COMPLETED"

            # Build final output
            result = {
                "status": status,
                "scaned_files": scaned_files,
                "issues": [issue.to_dict() for issue in unique_issues],
            }

            LOGGER.info(
                "Final result: status=%s, files=%d, issues=%d",
                status,
                scaned_files,
                len(unique_issues),
            )

            # Return final state
            return {
                "status": status,
                "final_output": result,
            }

        except Exception as e:
            LOGGER.exception("Merge node failed")
            return {
                "status": "FAILED",
                "scaned_files": state.get("scaned_files", 0),
                "issues": [],
                "error": str(e),
                "final_output": {
                    "status": "FAILED",
                    "scaned_files": state.get("scaned_files", 0),
                    "issues": [],
                },
            }

    def _semantic_deduplicate(
        self,
        issues: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        """Use a compact LLM pass to group semantic duplicates."""
        if self._model is None or len(issues) < 2:
            return issues

        candidates = self._build_semantic_candidates(issues)
        if len(candidates) < 2:
            return issues

        try:
            groups = self._request_duplicate_groups(candidates)
            return self._apply_duplicate_groups(issues, groups)
        except Exception as exc:
            LOGGER.warning(
                "Semantic findings dedup failed; using deterministic result: %s",
                exc,
            )
            return issues

    def _build_semantic_candidates(
        self,
        issues: list[ExpertIssue],
    ) -> list[dict[str, Any]]:
        """Build minimal same-file candidates for low-token semantic grouping."""
        path_counts: dict[str, int] = {}
        for issue in issues:
            path_counts[issue.path] = path_counts.get(issue.path, 0) + 1

        candidates = []
        for idx, issue in enumerate(issues):
            if path_counts.get(issue.path, 0) < 2:
                continue
            candidates.append(
                {
                    "id": idx,
                    "title": issue.title[:120],
                    "path": issue.path,
                    "line": issue.line,
                    "severity": issue.severity,
                    "summary": issue.summary[:160],
                    "code": self._compact_text(issue.code, 220),
                }
            )
        return candidates

    def _request_duplicate_groups(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[list[int]]:
        prompt = (
            "Group duplicate security findings. Return ONLY JSON: "
            '{"duplicate_groups":[[id,id]]}. '
            "Duplicates describe the same root vulnerability/fix. "
            "Do not group separate risks. Candidates:\n"
            f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}"
        )
        response = self._model.invoke([HumanMessage(content=prompt)])
        content = getattr(response, "content", response)
        data = self._parse_json_object(str(content))
        groups = data.get("duplicate_groups", [])
        if not isinstance(groups, list):
            return []
        return groups

    def _apply_duplicate_groups(
        self,
        issues: list[ExpertIssue],
        groups: list[list[int]],
    ) -> list[ExpertIssue]:
        merged_by_first_id: dict[int, ExpertIssue] = {}
        consumed: set[int] = set()

        for raw_group in groups:
            group = [idx for idx in raw_group if isinstance(idx, int)]
            group = [
                idx for idx in group if 0 <= idx < len(issues) and idx not in consumed
            ]
            if len(group) < 2:
                continue
            primary = issues[group[0]]
            for idx in group[1:]:
                primary = self._merge_semantic_pair(primary, issues[idx])
            first_id = group[0]
            merged_by_first_id[first_id] = primary
            consumed.update(group)

        result = []
        for idx, issue in enumerate(issues):
            if idx in merged_by_first_id:
                result.append(merged_by_first_id[idx])
            elif idx not in consumed:
                result.append(issue)
        return result

    def _merge_semantic_pair(
        self,
        existing: ExpertIssue,
        issue: ExpertIssue,
    ) -> ExpertIssue:
        primary = self._choose_primary_issue(existing, issue)
        severity = self._merge_severities(existing.severity, issue.severity)

        if severity == primary.severity:
            return primary

        return ExpertIssue(
            title=primary.title,
            description=primary.description,
            severity=severity,
            category=primary.category,
            path=primary.path,
            line=primary.line,
            summary=primary.summary,
            code=primary.code,
            recommendation=primary.recommendation,
            metadata=primary.metadata,
        )

    def _choose_primary_issue(
        self,
        existing: ExpertIssue,
        issue: ExpertIssue,
    ) -> ExpertIssue:
        existing_code = self._normalize_code(existing.code)
        issue_code = self._normalize_code(issue.code)
        if len(issue_code) > len(existing_code):
            return issue
        if issue_code and not existing_code:
            return issue
        return existing

    @staticmethod
    def _merge_severities(sev1: str, sev2: str) -> str:
        severity_order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
        if severity_order.get(sev1, 0) >= severity_order.get(sev2, 0):
            return sev1
        return sev2

    @staticmethod
    def _normalize_code(code: str) -> str:
        return " ".join((code or "").split())

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        normalized = " ".join((text or "").split())
        return normalized[:limit]

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in semantic dedup response")
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("Semantic dedup response must be a JSON object")
        return data

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

            # Create deterministic fallback first; model consolidation is best-effort.
            merger = FindingsMerger()
            merger.add_expert_result(ExpertResult(expert_name="merged", issues=issues))
            fallback_issues = merger.get_merged_issues()
            unique_issues = self._consolidate_findings(issues, fallback_issues)

            LOGGER.info("After consolidation: %d unique issues", len(unique_issues))

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

    def _consolidate_findings(
        self,
        issues: list[ExpertIssue],
        fallback_issues: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        """Use the model to produce a final consolidated findings list."""
        if self._model is None or len(issues) < 2:
            return fallback_issues

        candidates = self._build_consolidation_candidates(issues)
        if len(candidates) < 2:
            return fallback_issues

        try:
            return self._request_consolidated_issues(candidates, fallback_issues)
        except Exception as exc:
            LOGGER.warning(
                "Findings consolidation failed; using deterministic result: %s",
                exc,
            )
            return fallback_issues

    def _build_consolidation_candidates(
        self,
        issues: list[ExpertIssue],
    ) -> list[dict[str, Any]]:
        """Build compact candidates for low-token findings consolidation."""
        candidates = []
        for idx, issue in enumerate(issues):
            candidates.append(
                {
                    "id": idx,
                    "title": issue.title[:120],
                    "description": self._compact_text(issue.description, 260),
                    "path": issue.path,
                    "line": issue.line,
                    "severity": issue.severity,
                    "category": issue.category[:80],
                    "summary": issue.summary[:160],
                    "code": self._compact_text(issue.code, 220),
                    "recommendation": self._compact_text(issue.recommendation, 260),
                }
            )
        return candidates

    def _request_consolidated_issues(
        self,
        candidates: list[dict[str, Any]],
        fallback_issues: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        prompt = (
            "Consolidate security findings into a final report list. "
            "Return ONLY JSON with this shape: "
            '{"issues":[{"title":"","description":"","severity":"HIGH",'
            '"category":"","path":"","line":1,"summary":"","code":"",'
            '"recommendation":""}]}. '
            "Merge findings that describe the same root vulnerability, combining "
            "useful context and remediation. Keep separate risks separate. "
            "Use only paths, lines, and code snippets present in candidates. "
            "Use the highest severity among merged findings. Candidates:\n"
            f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}"
        )
        response = self._model.invoke([HumanMessage(content=prompt)])
        content = getattr(response, "content", response)
        data = self._parse_json_object(str(content))
        consolidated = data.get("issues", [])
        if not isinstance(consolidated, list):
            raise ValueError("Consolidation response issues must be a list")
        if not consolidated:
            return fallback_issues
        issues = [self._issue_from_consolidated_dict(issue) for issue in consolidated]
        self._validate_consolidated_evidence(issues, candidates)
        return issues

    @staticmethod
    def _issue_from_consolidated_dict(data: dict[str, Any]) -> ExpertIssue:
        if not isinstance(data, dict):
            raise ValueError("Consolidated issue must be an object")
        required_fields = {
            "title",
            "description",
            "severity",
            "category",
            "path",
            "line",
            "summary",
            "code",
            "recommendation",
        }
        missing = required_fields - set(data.keys())
        if missing:
            raise ValueError(f"Consolidated issue missing fields: {sorted(missing)}")
        return ExpertIssue(
            title=str(data["title"]),
            description=str(data["description"]),
            severity=str(data["severity"]),
            category=str(data["category"]),
            path=str(data["path"]),
            line=int(data["line"]),
            summary=str(data["summary"]),
            code=str(data["code"]),
            recommendation=str(data["recommendation"]),
        )

    def _validate_consolidated_evidence(
        self,
        issues: list[ExpertIssue],
        candidates: list[dict[str, Any]],
    ) -> None:
        locations = {(candidate["path"], candidate["line"]) for candidate in candidates}
        codes_by_location: dict[tuple[str, int], set[str]] = {}
        for candidate in candidates:
            location = (candidate["path"], candidate["line"])
            code = self._normalize_code(str(candidate.get("code", "")))
            if code:
                codes_by_location.setdefault(location, set()).add(code)

        for issue in issues:
            location = (issue.path, issue.line)
            if location not in locations:
                raise ValueError(
                    f"Consolidated issue invented location: {issue.path}:{issue.line}"
                )
            code = self._normalize_code(issue.code)
            allowed_codes = codes_by_location.get(location, set())
            if code and code not in allowed_codes:
                raise ValueError("Consolidated issue invented code evidence")

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

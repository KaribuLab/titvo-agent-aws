"""Merge Findings Node for LangGraph workflow.

Final node that deduplicates and determines final status.
"""

import logging
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertResult
from code_analysis.domain.services.findings_merger import FindingsMerger
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)


class MergeFindingsNode:
    """Node for merging expert findings and determining final status."""

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
            unique_issues = merger.get_merged_issues()

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

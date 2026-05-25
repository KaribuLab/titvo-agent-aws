"""Base expert node for LangGraph workflow.

Provides common functionality for all security expert nodes.
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from code_analysis import prompts as prompt_registry
from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)


class BaseExpertNode(ABC):
    """Abstract base for expert analysis nodes.

    Each expert node:
    1. Filters files based on expert-specific patterns
    2. Formats files for analysis
    3. Invokes LLM with expert prompt
    4. Parses JSON response into ExpertResult
    """

    def __init__(self, model: BaseChatModel):
        self._model = model

    @property
    @abstractmethod
    def expert_name(self) -> str:
        """Return the expert's identifier name."""
        pass

    def get_file_patterns(self) -> list[str]:
        """Return file patterns this expert analyzes.

        Return empty list to analyze all files.
        """
        return []

    def should_analyze_file(self, file_path: str) -> bool:
        """Check if file should be analyzed by this expert."""
        patterns = self.get_file_patterns()
        if not patterns:
            return True

        import fnmatch
        return any(
            fnmatch.fnmatch(file_path.lower(), p.lower())
            for p in patterns
        )

    def _filter_files(
        self,
        files: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Filter files based on expert patterns."""
        patterns = self.get_file_patterns()
        if not patterns:
            return files

        filtered = [
            f for f in files
            if self.should_analyze_file(f["path"])
        ]

        # Fallback: if nothing matched, analyze all
        if not filtered and files:
            LOGGER.debug(
                "No files matched patterns %s for %s, using fallback",
                patterns,
                self.expert_name,
            )
            return files

        return filtered

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        """Execute expert analysis.

        Args:
            state: Current workflow state with files

        Returns:
            State updates with new issues
        """
        try:
            # Filter files for this expert
            files = state.get("files", [])
            filtered_files = self._filter_files(files)

            LOGGER.info(
                "%s analyzing %d files (%d total)",
                self.expert_name,
                len(filtered_files),
                len(files),
            )

            if not filtered_files:
                LOGGER.debug("No files to analyze for %s", self.expert_name)
                return {"issues": []}

            # Format files for LLM
            files_content = self._format_files(filtered_files)

            # Get expert prompt
            expert_prompt = prompt_registry.get_expert_prompt(self.expert_name)

            # Create messages
            system_msg = SystemMessage(content=expert_prompt)
            human_msg = HumanMessage(content=files_content)

            # Invoke LLM
            LOGGER.debug("Invoking %s expert", self.expert_name)
            response = await self._model.ainvoke([system_msg, human_msg])

            # Parse response
            result = self._parse_response(response.content, filtered_files)

            LOGGER.info(
                "%s found %d issues",
                self.expert_name,
                len(result.issues),
            )

            # Return issues to merge into state
            return {
                "issues": state.get("issues", []) + result.issues,
                "expert_metadata": {
                    **state.get("expert_metadata", {}),
                    self.expert_name: {
                        "files_analyzed": len(filtered_files),
                        "issues_found": len(result.issues),
                    },
                },
            }

        except Exception as e:
            LOGGER.exception("Expert %s failed", self.expert_name)
            # Record error but continue workflow
            return {
                "issues": state.get("issues", []),
                "expert_errors": [
                    *state.get("expert_errors", []),
                    f"{self.expert_name}: {e}",
                ],
                "expert_metadata": {
                    **state.get("expert_metadata", {}),
                    self.expert_name: {"error": str(e)},
                },
            }

    def _format_files(self, files: list[dict[str, str]]) -> str:
        """Format files for LLM consumption."""
        parts = []
        for f in files:
            parts.append(f"=== FILE: {f['path']} ===")
            parts.append(f["content"])
            parts.append("=== END FILE ===")
            parts.append("")
        return "\n".join(parts)

    def _parse_response(
        self,
        content: str | list[Any],
        files: list[dict[str, str]],
    ) -> ExpertResult:
        """Parse LLM response into ExpertResult."""
        # Handle content that might be a list (OpenAI Responses API)
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    text_parts.append(block["text"])
            content = "".join(text_parts)

        content = str(content).strip()

        # Try to extract JSON from markdown fences
        if content.startswith("```json"):
            content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
        elif content.startswith("```"):
            content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            LOGGER.warning(
                "Failed to parse JSON from %s response: %s",
                self.expert_name,
                content[:200],
            )
            return ExpertResult(
                expert_name=self.expert_name,
                issues=[],
                error="Failed to parse JSON response",
                files_analyzed=len(files),
            )

        # Extract issues
        issues_data = data.get("issues", [])
        if not isinstance(issues_data, list):
            LOGGER.warning(
                "Invalid issues format from %s: %s",
                self.expert_name,
                type(issues_data),
            )
            issues_data = []

        issues = []
        for issue_data in issues_data:
            try:
                issue = ExpertIssue.from_dict(issue_data)
                issues.append(issue)
            except Exception as e:
                LOGGER.warning(
                    "Failed to parse issue from %s: %s - %s",
                    self.expert_name,
                    e,
                    issue_data,
                )

        return ExpertResult(
            expert_name=self.expert_name,
            issues=issues,
            files_analyzed=len(files),
        )

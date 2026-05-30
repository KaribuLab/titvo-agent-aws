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
from code_analysis.infra.adapters.langgraph.nodes._structural_lines import is_structural
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

# Adaptive per-file character budget based on commit size.
# Ensures the total prompt stays within the model's context window.
# Ratios: chars ÷ 4 ≈ tokens (rough estimate).


def _max_file_chars(num_files: int) -> int:
    """Return per-file char budget so total prompt fits in a 128k-token model."""
    if num_files <= 5:
        return 30_000   # ≈ 7 500 tokens/file → ~37 k total
    if num_files <= 10:
        return 15_000   # ≈ 3 750 tokens/file → ~37 k total
    if num_files <= 20:
        return 8_000    # ≈ 2 000 tokens/file → ~40 k total
    if num_files <= 40:
        return 5_000    # ≈ 1 250 tokens/file → ~50 k total
    return 3_000        # ≈  750 tokens/file → fits very large commits


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

            # Format commit files for LLM
            files_content = self._format_files(filtered_files)

            # Filter RAG chunks by this expert's file patterns and append
            all_rag_chunks = state.get("rag_chunks", [])
            filtered_rag = [
                c for c in all_rag_chunks
                if self.should_analyze_file(c.get("file_path", ""))
            ]
            rag_content = self._format_rag_chunks(filtered_rag)

            # Get expert prompt
            expert_prompt = prompt_registry.get_expert_prompt(self.expert_name)

            # Create messages
            system_msg = SystemMessage(content=expert_prompt)
            human_msg = HumanMessage(content=files_content + rag_content)

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
        """Format commit files for LLM consumption.

        Uses an adaptive per-file char budget (smaller budget for large commits)
        and structure-aware truncation so that even truncated files preserve
        imports + function/class signatures alongside as much body as fits.
        """
        limit = _max_file_chars(len(files))
        parts = []
        for f in files:
            content, truncated = self._smart_truncate(f["content"], limit)
            parts.append(f"=== FILE: {f['path']} ===")
            parts.append(content)
            if truncated:
                parts.append(
                    "[... file truncated: structural signature preserved above ...]"
                )
            parts.append("=== END FILE ===")
            parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _smart_truncate(content: str, max_chars: int) -> tuple[str, bool]:
        """Truncate file content while preserving structural lines.

        Strategy (when content > max_chars):
        - First 70% of budget: verbatim content from the beginning
          (imports + first functions/classes are typically here).
        - Remaining 30%: structural-only summary of what was cut
          (function/class signatures from the skipped portion).

        This ensures the LLM always sees:
        1. All imports and early function bodies (complete logic flow).
        2. The names and signatures of any functions/classes it can't read in full.
        """
        if len(content) <= max_chars:
            return content, False

        head_limit = max_chars * 7 // 10
        # Extend to last complete line within head_limit
        raw_head = content[:head_limit]
        last_nl = raw_head.rfind("\n")
        head = content[: last_nl + 1] if last_nl > 0 else raw_head

        rest = content[len(head):]
        tail_budget = max_chars - len(head)

        structural_lines = [
            line for line in rest.splitlines() if is_structural(line)
        ]
        tail = "\n".join(structural_lines)[:tail_budget]

        if tail:
            omitted = len(rest) - len(tail)
            separator = (
                f"\n# [{omitted:,} chars omitted"
                " — structural overview of remainder]\n"
            )
            return head + separator + tail, True

        return head, True

    def _format_rag_chunks(self, chunks: list[dict]) -> str:
        """Format RAG context chunks for LLM consumption.

        Returns empty string when chunks list is empty so no block is added.
        """
        if not chunks:
            return ""
        parts = ["\n=== RAG CONTEXT (codebase background) ==="]
        for chunk in chunks:
            parts.append(f"--- {chunk.get('file_path', 'unknown')} ---")
            parts.append(chunk.get("chunk_text", ""))
        parts.append("=== END RAG CONTEXT ===\n")
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

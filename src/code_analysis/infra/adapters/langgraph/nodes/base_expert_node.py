"""Base expert node for LangGraph workflow.

Each expert:
1. Selects files whose ``runtimes`` intersect its own runtime set (no fallback).
2. Splits them into chunks (large files) and packs the chunks into batches.
3. Runs one LLM call per batch, concurrently under a shared semaphore, with
   retries on provider errors.
4. Parses every batch into ``ExpertIssue`` objects, mapping chunk-relative
   line numbers back to the original file.
5. Returns only its delta: issues, errors, failed batches and metadata.
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from code_analysis import prompts as prompt_registry
from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult
from code_analysis.domain.services.batch_planner import (
    DEFAULT_BATCH_BUDGET_CHARS,
    DEFAULT_CHUNK_OVERLAP_CHARS,
    DEFAULT_PER_FILE_CAP_CHARS,
    Batch,
    Chunk,
    plan_files,
)
from code_analysis.domain.services.runtime_classifier import (
    ProjectProfile,
    Runtime,
    classify_values,
)
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

ALL_RUNTIME_VALUES: frozenset[str] = frozenset(r.value for r in Runtime)

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0

ENV_FILE_CAP = "TITVO_EXPERT_FILE_CAP_CHARS"
ENV_BATCH_BUDGET = "TITVO_EXPERT_BATCH_BUDGET_CHARS"
ENV_CHUNK_OVERLAP = "TITVO_EXPERT_CHUNK_OVERLAP_CHARS"
ENV_MAX_CONCURRENCY = "TITVO_EXPERT_MAX_CONCURRENCY"


@dataclass
class ExpertRuntimeConfig:
    """Tunables shared by every expert node (one instance per workflow)."""

    per_file_cap_chars: int = DEFAULT_PER_FILE_CAP_CHARS
    batch_budget_chars: int = DEFAULT_BATCH_BUDGET_CHARS
    chunk_overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, self.max_concurrency))
        return self._semaphore

    @classmethod
    def from_env(cls) -> "ExpertRuntimeConfig":
        """Read tunables from the environment, falling back to defaults."""
        return cls(
            per_file_cap_chars=_env_int(ENV_FILE_CAP, DEFAULT_PER_FILE_CAP_CHARS),
            batch_budget_chars=_env_int(ENV_BATCH_BUDGET, DEFAULT_BATCH_BUDGET_CHARS),
            chunk_overlap_chars=_env_int(
                ENV_CHUNK_OVERLAP, DEFAULT_CHUNK_OVERLAP_CHARS
            ),
            max_concurrency=_env_int(ENV_MAX_CONCURRENCY, DEFAULT_MAX_CONCURRENCY),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "per_file_cap_chars": self.per_file_cap_chars,
            "batch_budget_chars": self.batch_budget_chars,
            "chunk_overlap_chars": self.chunk_overlap_chars,
            "max_concurrency": self.max_concurrency,
        }


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        LOGGER.warning(
            "Invalid integer for %s=%r; using default %d", name, raw, default
        )
        return default
    if value <= 0:
        LOGGER.warning(
            "Non-positive value for %s=%d; using default %d", name, value, default
        )
        return default
    return value


@dataclass
class _BatchOutcome:
    batch: Batch
    issues: list[ExpertIssue] = field(default_factory=list)
    error: str | None = None


class BaseExpertNode(ABC):
    """Abstract base for expert analysis nodes."""

    def __init__(
        self,
        model: BaseChatModel,
        config: ExpertRuntimeConfig | None = None,
    ):
        self._model = model
        self._config = config or ExpertRuntimeConfig()

    @property
    @abstractmethod
    def expert_name(self) -> str:
        """Return the expert's identifier name."""

    def get_runtimes(self) -> set[str]:
        """Runtimes this expert analyses. Default: every runtime."""
        return set(ALL_RUNTIME_VALUES)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def matches_runtimes(self, runtimes: list[str] | None) -> bool:
        """True when the file's runtime list intersects this expert's set."""
        effective = list(runtimes) if runtimes else [Runtime.UNKNOWN.value]
        return bool(set(effective) & self.get_runtimes())

    def _select_files(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [f for f in files if self.matches_runtimes(f.get("runtimes"))]

    def _select_rag_chunks(
        self,
        chunks: list[dict[str, Any]],
        profile: ProjectProfile,
    ) -> list[dict[str, Any]]:
        selected = []
        for chunk in chunks:
            runtimes = classify_values(
                chunk.get("file_path", ""), chunk.get("chunk_text", ""), profile
            )
            if self.matches_runtimes(runtimes):
                selected.append(chunk)
        return selected

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        files = state.get("files", []) or []
        selected = self._select_files(files)

        LOGGER.info(
            "%s selected %d of %d files",
            self.expert_name,
            len(selected),
            len(files),
        )

        if not selected:
            return {
                "expert_metadata": {
                    self.expert_name: {"files_analyzed": 0, "skipped": True},
                }
            }

        try:
            batches = plan_files(
                selected,
                per_file_cap=self._config.per_file_cap_chars,
                batch_budget=self._config.batch_budget_chars,
                overlap_chars=self._config.chunk_overlap_chars,
            )
            profile = ProjectProfile(
                **(state.get("expert_metadata", {}) or {}).get("project_profile", {})
            )
            rag_content = self._format_rag_chunks(
                self._select_rag_chunks(state.get("rag_chunks", []) or [], profile)
            )
            expert_prompt = prompt_registry.get_expert_prompt(self.expert_name)

            LOGGER.info(
                "%s running %d batches over %d chunks",
                self.expert_name,
                len(batches),
                sum(len(b.chunks) for b in batches),
            )
            outcomes = await asyncio.gather(
                *(self._run_batch(b, expert_prompt, rag_content) for b in batches)
            )
        except Exception as exc:  # noqa: BLE001 - expert failure must not kill the scan
            LOGGER.exception(
                "Expert %s failed before running batches", self.expert_name
            )
            paths = sorted({f["path"] for f in selected})
            return {
                "expert_errors": [f"{self.expert_name}: {exc}"],
                "failed_batches": [
                    {
                        "expert": self.expert_name,
                        "batch_index": -1,
                        "paths": paths,
                        "error": str(exc),
                    }
                ],
                "expert_metadata": {
                    self.expert_name: {
                        "files_analyzed": len(selected),
                        "batches": 0,
                        "failed_batches": 1,
                        "error": str(exc),
                    }
                },
            }

        issues: list[ExpertIssue] = []
        errors: list[str] = []
        failed: list[dict[str, Any]] = []
        for outcome in sorted(outcomes, key=lambda o: o.batch.index):
            if outcome.error:
                errors.append(
                    f"{self.expert_name}: batch {outcome.batch.index} failed: "
                    f"{outcome.error}"
                )
                failed.append(
                    {
                        "expert": self.expert_name,
                        "batch_index": outcome.batch.index,
                        "paths": outcome.batch.paths,
                        "error": outcome.error,
                    }
                )
            else:
                issues.extend(outcome.issues)

        LOGGER.info(
            "%s found %d issues (%d/%d batches failed)",
            self.expert_name,
            len(issues),
            len(failed),
            len(batches),
        )
        return {
            "issues": issues,
            "expert_errors": errors,
            "failed_batches": failed,
            "expert_metadata": {
                self.expert_name: {
                    "files_analyzed": len(selected),
                    "batches": len(batches),
                    "failed_batches": len(failed),
                    "issues_found": len(issues),
                }
            },
        }

    async def _run_batch(
        self,
        batch: Batch,
        expert_prompt: str,
        rag_content: str,
    ) -> _BatchOutcome:
        messages = [
            SystemMessage(content=expert_prompt),
            HumanMessage(content=self._format_batch(batch) + rag_content),
        ]
        try:
            async with self._config.semaphore:
                response = await self._invoke_with_retry(messages, batch.index)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(
                "%s batch %d failed after %d attempts: %s",
                self.expert_name,
                batch.index,
                self._config.max_attempts,
                exc,
            )
            return _BatchOutcome(batch=batch, error=str(exc))

        result = self._parse_response(response.content, batch)
        if result.error:
            return _BatchOutcome(batch=batch, error=result.error)
        return _BatchOutcome(batch=batch, issues=result.issues)

    async def _invoke_with_retry(self, messages: list[Any], batch_index: int) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self._config.max_attempts):
            try:
                return await self._model.ainvoke(messages)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 >= self._config.max_attempts:
                    break
                delay = self._config.backoff_base_seconds * (2**attempt)
                LOGGER.warning(
                    "%s batch %d attempt %d/%d failed (%s); retrying in %.1fs",
                    self.expert_name,
                    batch_index,
                    attempt + 1,
                    self._config.max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_batch(self, batch: Batch) -> str:
        parts: list[str] = []
        for chunk in batch.chunks:
            parts.append(self._format_chunk_header(chunk))
            if chunk.prefix:
                parts.append(chunk.prefix.rstrip("\n"))
            parts.append(chunk.body)
            parts.append("=== END FILE ===")
            parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _format_chunk_header(chunk: Chunk) -> str:
        header = f"=== FILE: {chunk.path} [runtime: {chunk.primary_runtime}]"
        if chunk.is_partial:
            header += (
                f" [lines {chunk.start_line}-{chunk.end_line} "
                f"of {chunk.total_lines}] ===\n"
                f"# Chunk {chunk.chunk_index + 1} of {chunk.total_chunks}. "
                f"Report `line` as the absolute line number in the original file "
                f"(this chunk starts at line {chunk.start_line})."
            )
        else:
            header += " ==="
        return header

    def _format_files(self, files: list[dict[str, Any]]) -> str:
        """Format files as a single batch (test/debug helper)."""
        batches = plan_files(
            files,
            per_file_cap=self._config.per_file_cap_chars,
            batch_budget=10**12,
            overlap_chars=self._config.chunk_overlap_chars,
        )
        return "".join(self._format_batch(b) for b in batches)

    def _format_rag_chunks(self, chunks: list[dict]) -> str:
        if not chunks:
            return ""
        parts = ["\n=== RAG CONTEXT (codebase background) ==="]
        for chunk in chunks:
            parts.append(f"--- {chunk.get('file_path', 'unknown')} ---")
            parts.append(chunk.get("chunk_text", ""))
        parts.append("=== END RAG CONTEXT ===\n")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        content: str | list[Any],
        batch: Batch,
    ) -> ExpertResult:
        """Parse an LLM response for *batch* into an ExpertResult."""
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    text_parts.append(str(block["text"]))
            content = "".join(text_parts)

        content = str(content).strip()
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
                "Failed to parse JSON from %s batch %d: %s",
                self.expert_name,
                batch.index,
                content[:200],
            )
            return ExpertResult(
                expert_name=self.expert_name,
                issues=[],
                error="Failed to parse JSON response",
                files_analyzed=len(batch.paths),
            )

        issues_data = data.get("issues", []) if isinstance(data, dict) else []
        if not isinstance(issues_data, list):
            LOGGER.warning(
                "Invalid issues format from %s: %s", self.expert_name, type(issues_data)
            )
            issues_data = []

        issues: list[ExpertIssue] = []
        for issue_data in issues_data:
            try:
                issue = ExpertIssue.from_dict(issue_data)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Failed to parse issue from %s: %s - %s",
                    self.expert_name,
                    exc,
                    issue_data,
                )
                continue
            chunk = self._resolve_chunk(issue, batch)
            if chunk is not None:
                issue.line = self._resolve_line(issue.line, chunk)
            issue.metadata = {
                "expert": self.expert_name,
                "batch_index": batch.index,
                "chunk_index": chunk.chunk_index if chunk is not None else 0,
            }
            issues.append(issue)

        return ExpertResult(
            expert_name=self.expert_name,
            issues=issues,
            files_analyzed=len(batch.paths),
        )

    @staticmethod
    def _resolve_chunk(issue: ExpertIssue, batch: Batch) -> Chunk | None:
        candidates = [c for c in batch.chunks if c.path == issue.path]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        for chunk in candidates:
            if chunk.start_line <= issue.line <= chunk.end_line:
                return chunk
        return candidates[0]

    @staticmethod
    def _resolve_line(line: int, chunk: Chunk) -> int:
        """Map a reported line to the original file.

        The prompt asks for absolute line numbers; models sometimes answer
        relative to the chunk anyway. A value inside the chunk's absolute range
        is kept; a value that only fits the relative range is offset.
        """
        try:
            line = int(line)
        except (TypeError, ValueError):
            return 0
        if not chunk.is_partial or line <= 0:
            return line
        if chunk.start_line <= line <= chunk.end_line:
            return line
        relative_span = chunk.end_line - chunk.start_line + 1
        if 1 <= line <= relative_span:
            return chunk.start_line - 1 + line
        return line

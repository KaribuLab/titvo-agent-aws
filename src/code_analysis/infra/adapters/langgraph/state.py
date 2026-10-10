"""State definitions for LangGraph workflow.

Expert nodes run in parallel, so every key written by more than one node
carries a reducer: lists are concatenated and dicts are merged. Nodes return
only their delta; the reducer accumulates it into the shared state.
"""

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from code_analysis.domain.entities.expert_result import ExpertIssue


def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict:
    """Reducer for ``expert_metadata``: shallow merge, nested dicts merged one level."""
    merged: dict[str, Any] = dict(left or {})
    for key, value in (right or {}).items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    return merged


def keep_first(left: str | None, right: str | None) -> str | None:
    """Reducer for ``provider_error``: the first non-empty value wins."""
    return left if left else right


class AgentState(TypedDict):
    """State object passed between LangGraph nodes."""

    # Task identification
    task_id: str
    repository_url: str
    branch: str
    commit_hash: str
    extra_args: dict[str, Any]
    scan_mode: NotRequired[str]
    scan_ref: NotRequired[str]

    # MCP phase results. Each file: {"path", "content"} plus "runtimes"
    # (ordered list of runtime labels) once classify_runtime has run.
    files: list[dict[str, Any]]
    scaned_files: int
    mcp_error: NotRequired[str | None]

    # RAG context chunks (retrieved by RagRetrievalNode, consumed by expert nodes)
    rag_chunks: NotRequired[list[dict[str, Any]]]

    # Expert analysis results (reducers: experts run in parallel)
    issues: Annotated[list[ExpertIssue], operator.add]
    expert_errors: Annotated[list[str], operator.add]
    failed_batches: Annotated[list[dict[str, Any]], operator.add]
    expert_metadata: Annotated[dict[str, Any], merge_dicts]
    # Set by any expert that saw the provider breaker open (no credits, bad
    # key...). Makes `merge` end the scan as FAILED with an explicit error.
    provider_error: Annotated[str | None, keep_first]

    # Kept for backwards compatibility with older callers; unused.
    current_expert_index: NotRequired[int]

    # Final output
    status: NotRequired[str]  # COMPLETED, WARNING, FAILED
    error: NotRequired[str | None]
    final_output: NotRequired[dict[str, Any]]

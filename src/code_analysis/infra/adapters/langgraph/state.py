"""State definitions for LangGraph workflow.

This module defines the TypedDict state that flows through the LangGraph nodes.
"""

from typing import Any, NotRequired, TypedDict

from code_analysis.domain.entities.expert_result import ExpertIssue


class AgentState(TypedDict):
    """State object passed between LangGraph nodes.

    This TypedDict defines the complete state that flows through the
    workflow from MCP retrieval through expert analysis to final merge.
    """

    # Task identification
    task_id: str
    repository_url: str
    branch: str
    commit_hash: str
    extra_args: dict[str, Any]
    scan_mode: NotRequired[str]
    scan_ref: NotRequired[str]

    # MCP phase results
    files: list[dict[str, str]]  # List of {"path": str, "content": str}
    scaned_files: int
    mcp_error: NotRequired[str | None]

    # RAG context chunks (retrieved by RagRetrievalNode, consumed by expert nodes)
    rag_chunks: NotRequired[
        list[dict[str, Any]]
    ]  # {"file_path", "chunk_text", "distance"}

    # Expert analysis results
    issues: list[ExpertIssue]

    # Expert tracking (for sequential flow)
    current_expert_index: NotRequired[int]
    expert_errors: NotRequired[list[str]]

    # Final output
    status: NotRequired[str]  # COMPLETED, WARNING, FAILED
    error: NotRequired[str | None]
    final_output: NotRequired[dict[str, Any]]

    # Metadata for tracing
    expert_metadata: NotRequired[dict[str, Any]]

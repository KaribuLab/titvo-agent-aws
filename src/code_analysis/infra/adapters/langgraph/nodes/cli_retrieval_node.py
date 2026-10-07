"""Retrieve an uploaded CLI snapshot without contacting a Git provider."""

import asyncio
from typing import Any

from code_analysis.infra.adapters.cli_snapshot import CliSnapshotRepository
from code_analysis.infra.adapters.langgraph.state import AgentState


class CliRetrievalNode:
    """Adapt synchronous AWS snapshot reads to the existing workflow envelope."""

    def __init__(self, repository: CliSnapshotRepository):
        self.repository = repository

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        """Fail the entire retrieval if the uploaded snapshot is incomplete."""
        try:
            batch_id = state.get("extra_args", {}).get("batch_id", "")
            files = await asyncio.to_thread(self.repository.get_files, batch_id)
            return {
                "files": files,
                "scaned_files": len(files),
                "mcp_error": None,
                "rag_chunks": [],
            }
        except Exception as exc:
            return {
                "files": [],
                "scaned_files": 0,
                "mcp_error": f"CLI retrieval: {exc}",
            }

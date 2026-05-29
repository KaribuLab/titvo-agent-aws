"""RAG Retrieval Node for LangGraph workflow.

Executes after mcp_retrieve and before expert nodes. For each file in the
commit, queries the vector store for semantically related chunks from the
full branch codebase. Results are stored in state.rag_chunks for expert nodes.

On any error (index unavailable, S3 error, embedding error) returns
rag_chunks=[] so downstream experts continue with commit files only.
"""

import logging
from typing import Any

from code_analysis.domain.ports.rag_context_port import IRagContextPort
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

_MAX_CHUNKS_TOTAL = 30
_CHUNKS_PER_FILE = 3
_MAX_FILES_TO_QUERY = 10
_QUERY_CONTENT_CHARS = 400


class RagRetrievalNode:
    """Retrieves RAG context chunks for all commit files.

    For each file (up to _MAX_FILES_TO_QUERY), searches the vector store
    with a query of "{path}\\n{content[:400]}". Deduplicates by chunk_text
    and limits total chunks to _MAX_CHUNKS_TOTAL.
    """

    def __init__(self, rag_context: IRagContextPort):
        self._rag_context = rag_context

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        files = state.get("files", [])

        if not files:
            LOGGER.debug("[RAG Node] No files in state — skipping enrichment")
            return {"rag_chunks": []}

        repository_url = state.get("repository_url", "")
        branch = state.get("branch", "")
        try:
            self._rag_context.configure(repository_url, branch)
            chunks = self._retrieve_chunks(files)
            LOGGER.info("[RAG Node] Retrieved %d unique RAG chunks", len(chunks))
            return {"rag_chunks": chunks}
        except Exception:
            LOGGER.warning(
                "[RAG Node] Retrieval failed — continuing without RAG", exc_info=True
            )
            return {"rag_chunks": []}
        finally:
            try:
                self._rag_context.close()
            except Exception:
                LOGGER.warning("[RAG Node] close() failed", exc_info=True)

    def _retrieve_chunks(self, files: list[dict[str, str]]) -> list[dict[str, Any]]:
        seen_texts: set[str] = set()
        results: list[dict[str, Any]] = []

        for file in files[:_MAX_FILES_TO_QUERY]:
            if len(results) >= _MAX_CHUNKS_TOTAL:
                break

            query = f"{file['path']}\n{file['content'][:_QUERY_CONTENT_CHARS]}"
            chunks = self._rag_context.search(query, k=_CHUNKS_PER_FILE)

            for chunk in chunks:
                if len(results) >= _MAX_CHUNKS_TOTAL:
                    break
                text = chunk.get("chunk_text", "")
                if text and text not in seen_texts:
                    seen_texts.add(text)
                    results.append(chunk)

        return results

"""Port for retrieving RAG context chunks from the vector store."""

from abc import ABC, abstractmethod
from typing import Any


class IRagContextPort(ABC):
    """Port for searching the RAG vector index and retrieving related code chunks.

    Usage per job:
        1. Call configure(repository_url, branch) once before any search.
        2. Call search() as many times as needed.
        3. Call close() to release resources (temp files, etc.).
    """

    @abstractmethod
    def configure(self, repository_url: str, branch: str) -> None:
        """Set the repository and branch to download the index from.

        Must be called before search(). Safe to call multiple times with the
        same arguments (idempotent); re-configuring with different values
        resets the adapter.
        """

    @abstractmethod
    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        """Search for the k most semantically similar chunks to the query.

        Args:
            query: Search query (typically file path + content snippet).
            k: Maximum number of results to return.

        Returns:
            List of dicts with keys: file_path, chunk_text, distance.
            Returns empty list on any error (graceful degradation).
        """

    @abstractmethod
    def close(self) -> None:
        """Release resources (e.g. delete temporary index.db file)."""

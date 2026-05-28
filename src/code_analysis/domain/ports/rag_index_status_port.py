"""Port for checking whether a branch is indexed in the RAG store."""
from abc import ABC, abstractmethod


class IRagIndexStatusPort(ABC):
    @abstractmethod
    def is_indexed(self, repository_url: str, branch: str) -> bool:
        """Return True if the RAG index exists for the given repository and branch."""

    @abstractmethod
    def is_commit_indexed(
        self, repository_url: str, branch: str, commit_sha: str
    ) -> bool:
        """Return True if the given commit is already indexed for the branch."""

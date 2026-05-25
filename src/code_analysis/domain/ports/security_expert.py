"""Port interface for security expert implementations."""

from abc import ABC, abstractmethod
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertResult, FileContent


class SecurityExpertPort(ABC):
    """Abstract base class for security expert analyzers.
    
    Each expert specializes in a specific security domain (OWASP API,
    OWASP Web, DevSecOps, Code Vulnerabilities, Prompt Hardening).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the expert's unique name."""
        pass

    @property
    @abstractmethod
    def category(self) -> str:
        """Return the security category this expert handles."""
        pass

    @abstractmethod
    def get_file_patterns(self) -> list[str]:
        """Return file patterns this expert is interested in.
        
        Returns empty list if expert should analyze all files.
        Examples: ['*.py', '*routes*'], ['*.yml', 'Dockerfile']
        """
        pass

    @abstractmethod
    def should_analyze_file(self, file_path: str) -> bool:
        """Determine if this file should be analyzed by this expert.
        
        Args:
            file_path: Path to the file
            
        Returns:
            True if expert should analyze this file
        """
        pass

    @abstractmethod
    async def analyze(
        self,
        files: list[FileContent],
        repository_url: str,
        commit_hash: str,
        extra_args: dict[str, Any],
    ) -> ExpertResult:
        """Analyze files and return findings.
        
        Args:
            files: List of file contents to analyze
            repository_url: Source repository URL
            commit_hash: Commit being analyzed
            extra_args: Additional parameters from task
            
        Returns:
            ExpertResult with findings or error
        """
        pass

    def filter_files(self, files: list[FileContent]) -> list[FileContent]:
        """Filter files based on expert's patterns.
        
        If no patterns match, returns all files (fallback).
        """
        patterns = self.get_file_patterns()
        
        # No patterns defined - analyze all files
        if not patterns:
            return files

        filtered = [
            f for f in files
            if any(self._matches_pattern(f.path, p) for p in patterns)
        ]

        # Fallback: if nothing matched, analyze all files
        if not filtered:
            LOGGER.debug(
                "No files matched patterns %s for %s, using fallback",
                patterns,
                self.name,
            )
            return files

        return filtered

    def _matches_pattern(self, file_path: str, pattern: str) -> bool:
        """Check if file path matches pattern (simple glob support)."""
        import fnmatch
        return fnmatch.fnmatch(file_path.lower(), pattern.lower())


import logging  # noqa: E402

LOGGER = logging.getLogger(__name__)

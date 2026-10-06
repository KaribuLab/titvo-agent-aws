"""Compatibility re-export; the implementation lives in the domain layer."""

from code_analysis.domain.services.structural_lines import (  # noqa: F401
    extract_structural_lines,
    is_structural,
)

__all__ = ["extract_structural_lines", "is_structural"]

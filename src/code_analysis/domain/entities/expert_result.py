"""Domain entities for expert analysis results."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FileContent:
    """Represents a file with its content retrieved from MCP."""

    path: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content}


@dataclass
class ExpertIssue:
    """Represents a security issue found by an expert.
    
    Attributes:
        title: Brief title of the vulnerability
        description: Detailed description in Spanish
        severity: CRITICAL, HIGH, MEDIUM, or LOW
        category: OWASP category or security domain
        path: File path where issue was found
        line: Line number (optional)
        summary: Brief summary in Spanish
        code: Code snippet showing the vulnerability
        recommendation: How to fix the issue in Spanish
        metadata: Additional expert-specific data
    """

    title: str
    description: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    category: str
    path: str
    line: int
    summary: str
    code: str
    recommendation: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalize severity
        valid_severities = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        if self.severity.upper() not in valid_severities:
            self.severity = "MEDIUM"  # Default conservative
        else:
            self.severity = self.severity.upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "category": self.category,
            "path": self.path,
            "line": self.line,
            "summary": self.summary,
            "code": self.code,
            "recommendation": self.recommendation,
            **self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpertIssue":
        """Create from dictionary, handling extra fields as metadata."""
        known_fields = {
            "title", "description", "severity", "category",
            "path", "line", "summary", "code", "recommendation",
        }
        metadata = {k: v for k, v in data.items() if k not in known_fields}
        
        return cls(
            title=data.get("title", "Unknown"),
            description=data.get("description", ""),
            severity=data.get("severity", "MEDIUM"),
            category=data.get("category", "Unknown"),
            path=data.get("path", ""),
            line=data.get("line", 0),
            summary=data.get("summary", ""),
            code=data.get("code", ""),
            recommendation=data.get("recommendation", ""),
            metadata=metadata,
        )

    def get_dedup_key(self) -> tuple[str, int, str]:
        """Return key for deduplication: (path, line, category)."""
        return (self.path, self.line, self.category)


@dataclass
class ExpertResult:
    """Result from a single expert analysis.
    
    Attributes:
        expert_name: Name of the expert that produced this result
        issues: List of security issues found
        error: Optional error message if analysis failed
        files_analyzed: Number of files analyzed
    """

    expert_name: str
    issues: list[ExpertIssue] = field(default_factory=list)
    error: Optional[str] = None
    files_analyzed: int = 0

    def is_success(self) -> bool:
        """Check if expert completed without error."""
        return self.error is None

    def has_findings(self) -> bool:
        """Check if any issues were found."""
        return len(self.issues) > 0

    def get_max_severity(self) -> Optional[str]:
        """Get highest severity found."""
        if not self.issues:
            return None
        severity_order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
        return max(
            self.issues,
            key=lambda i: severity_order.get(i.severity, 0)
        ).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_name": self.expert_name,
            "issues": [issue.to_dict() for issue in self.issues],
            "error": self.error,
            "files_analyzed": self.files_analyzed,
        }

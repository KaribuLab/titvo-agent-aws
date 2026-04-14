from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class AnalysisStatus(Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WARNING = "WARNING"



@dataclass
class IssueDto:
    path: str
    line: int
    title: str
    description: str
    severity: str
    type: str
    code: str
    summary: str
    recommendation: str

@dataclass
class ResultDto:
    source: str
    args: Dict[str, Any]
    commit_hash: str
    status: str
    scaned_files: int
    issues: list[IssueDto]
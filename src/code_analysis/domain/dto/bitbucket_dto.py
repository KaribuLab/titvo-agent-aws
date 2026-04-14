from dataclasses import dataclass
from typing import List

from code_analysis.domain.dto.result_dto import IssueDto


@dataclass
class BitbucketCodeInsightsInputDto:
    reportURL: str
    workspaceId: str
    commitHash: str
    repoSlug: str
    status: str
    annotations: List[IssueDto]
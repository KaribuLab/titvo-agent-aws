from abc import ABC, abstractmethod
from typing import Any, Dict

from code_analysis.domain.dto.result_dto import ResultDto


class IGitHubRepository(ABC):
    @abstractmethod
    def create_github_issue(self, result: ResultDto) -> Dict[str, Any]:
        pass

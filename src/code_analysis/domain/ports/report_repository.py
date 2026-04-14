from abc import ABC, abstractmethod
from typing import Any, Dict

from code_analysis.domain.dto.result_dto import ResultDto


class IReportRepository(ABC):
    @abstractmethod
    def create_report(self, result_dto: ResultDto) -> Dict[str, Any]:
        pass

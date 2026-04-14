from abc import ABC, abstractmethod
from typing import Any, Dict

from code_analysis.domain.dto.bitbucket_dto import BitbucketCodeInsightsInputDto


class IBitbucketRepository(ABC):
    @abstractmethod
    def create_code_insights_report(
        self, bitbucket_code_insights_input_dto: BitbucketCodeInsightsInputDto
    ) -> Dict[str, Any]:
        pass

import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Dict

from boto3.session import boto3

from code_analysis.domain.dto.bitbucket_dto import BitbucketCodeInsightsInputDto
from code_analysis.domain.ports.bitbucket_repository import (
    IBitbucketRepository,
)
from code_analysis.infra.adapters.lambda_payload_json import dumps_lambda_payload

LOGGER = logging.getLogger(__name__)


class LambdaBitbucketRepository(IBitbucketRepository):
    def __init__(self, function_name: str):
        self.lambda_client = boto3.client("lambda")
        self.function_name = function_name

    def create_code_insights_report(
        self, bitbucket_code_insights_input_dto: BitbucketCodeInsightsInputDto
    ) -> Dict[str, Any]:
        input_payload = {
            "reportURL": bitbucket_code_insights_input_dto.reportURL,
            "workspaceId": bitbucket_code_insights_input_dto.workspaceId,
            "commitHash": bitbucket_code_insights_input_dto.commitHash,
            "repoSlug": bitbucket_code_insights_input_dto.repoSlug,
            "status": bitbucket_code_insights_input_dto.status,
            "annotations": [
                asdict(issue)
                if is_dataclass(issue) and not isinstance(issue, type)
                else issue
                for issue in bitbucket_code_insights_input_dto.annotations
            ],
        }
        response = self.lambda_client.invoke(
            FunctionName=self.function_name,
            Payload=dumps_lambda_payload(input_payload),
        )
        if response["StatusCode"] != 200:
            raise Exception(
                f"Failed to create report: {response['Payload'].read().decode('utf-8')}"
            )

        output_payload = response["Payload"].read().decode("utf-8")
        LOGGER.info(
            "Code insights report created successfully %s",
            output_payload,
        )
        return json.loads(output_payload)

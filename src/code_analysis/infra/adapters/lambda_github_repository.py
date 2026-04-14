import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Dict

from boto3.session import boto3

from code_analysis.domain.dto.result_dto import ResultDto
from code_analysis.domain.ports.github_repository import IGitHubRepository
from code_analysis.infra.adapters.lambda_payload_json import dumps_lambda_payload

LOGGER = logging.getLogger(__name__)

class LambdaGitHubRepository(IGitHubRepository):
    def __init__(self, function_name: str):
        self.lambda_client = boto3.client("lambda")
        self.function_name = function_name

    def create_github_issue(self, result_dto: ResultDto) -> Dict[str, Any]:
        repository_slug = result_dto.args.get("repository_slug").split("/")
        repo_owner = repository_slug[0]
        repo_name = repository_slug[1]
        input_payload = {
            "repoOwner": repo_owner,
            "repoName": repo_name,
            "asignee": result_dto.args.get("github_assignee"),
            "commitHash": result_dto.args.get("github_commit_sha"),
            "status": result_dto.status,
            "annotations": [
                asdict(issue)
                if is_dataclass(issue) and not isinstance(issue, type)
                else issue
                for issue in result_dto.issues
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
            "GitHub issue created successfully %s", output_payload,
        )
        return json.loads(output_payload)
import asyncio
import logging
import os
from ast import Dict
from logging.config import dictConfig
from typing import Any, Optional

import boto3
from langchain_mcp_adapters.client import MultiServerMCPClient
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from code_analysis.application.analyse_code_use_case import AnalyseCodeUseCase
from code_analysis.domain.notification_service import NotificationService
from code_analysis.infra.adapters.dynamo_task_repository import DynamoTaskRepository
from code_analysis.infra.adapters.lambda_bitbucket_repository import (
    LambdaBitbucketRepository,
)
from code_analysis.infra.adapters.lambda_github_repository import (
    LambdaGitHubRepository,
)
from code_analysis.infra.adapters.lambda_report_repository import (
    LambdaReportRepository,
)
from code_analysis.infra.adapters.langchain_agent_adapter import (
    AsyncMCPToolsFactory,
    LangchainAgent,
    LangchainAgentModelFactory,
)
from logging_config import config
from shared.infra.adapters.aws_configuration_adapter import AwsConfigurationAdapter
from shared.infra.adapters.aws_secrets_adapter import AwsSecretsAdapter
from shared.infra.services.encryption_service import EncryptionService

dictConfig(config)

LOGGER = logging.getLogger(__name__)


def create_boto3_client(service_name: str) -> Any:
    aws_endpoint = os.getenv("AWS_ENDPOINT")
    if aws_endpoint is not None:
        return boto3.client(service_name, endpoint_url=aws_endpoint)
    return boto3.client(service_name)


async def main():
    task_id = os.getenv("TITVO_SCAN_TASK_ID")
    LOGGER.debug("Starting the application with task id %s", task_id)
    if task_id is None:
        raise ValueError("TITVO_SCAN_TASK_ID is not set")
    task_table_name = os.getenv("TITVO_DYNAMO_TASK_TABLE_NAME")
    LOGGER.debug("Task table name %s", task_table_name)
    if task_table_name is None:
        raise ValueError("TITVO_DYNAMO_TASK_TABLE_NAME is not set")
    config_table_name = os.getenv("TITVO_DYNAMO_CONFIGURATION_TABLE_NAME")
    LOGGER.debug("Config table name %s", config_table_name)
    if config_table_name is None:
        raise ValueError("TITVO_DYNAMO_CONFIGURATION_TABLE_NAME is not set")
    encryption_key_name = os.getenv("TITVO_ENCRYPTION_KEY_NAME")
    LOGGER.debug("Encryption key name %s", encryption_key_name)
    bitbucket_repository_function_name = os.getenv(
        "TITVO_BITBUCKET_CODE_INSIGHTS_FUNCTION_NAME"
    )
    LOGGER.debug(
        "Bitbucket repository function name %s", bitbucket_repository_function_name
    )
    if bitbucket_repository_function_name is None:
        raise ValueError("TITVO_BITBUCKET_CODE_INSIGHTS_FUNCTION_NAME is not set")
    github_repository_function_name = os.getenv("TITVO_GITHUB_ISSUE_FUNCTION_NAME")
    LOGGER.debug("Github repository function name %s", github_repository_function_name)
    if github_repository_function_name is None:
        raise ValueError("TITVO_GITHUB_ISSUE_FUNCTION_NAME is not set")
    report_repository_function_name = os.getenv("TITVO_REPORT_FUNCTION_NAME")
    LOGGER.debug("Report repository function name %s", report_repository_function_name)
    if report_repository_function_name is None:
        raise ValueError("TITVO_REPORT_FUNCTION_NAME is not set")
    if encryption_key_name is None:
        raise ValueError("TITVO_ENCRYPTION_KEY_NAME is not set")
    LOGGER.debug("Creating configuration provider")
    configuration_provider = AwsConfigurationAdapter(
        dynamodb_client=create_boto3_client("dynamodb"),
        table_name=config_table_name,
        encryption_service=EncryptionService(
            secrets_provider=AwsSecretsAdapter(
                client=create_boto3_client("secretsmanager"),
                key_name=encryption_key_name,
            ),
        ),
    )
    mcp_server_url = configuration_provider.get_value("mcp_server_url")
    LOGGER.debug("MCP server url %s", mcp_server_url)
    if mcp_server_url is None:
        raise ValueError("mcp_server_url is not set")
    system_prompt = configuration_provider.get_value("scan_system_prompt")
    LOGGER.debug("System prompt %s", system_prompt)
    if system_prompt is None:
        raise ValueError("system_prompt is not set")
    content_template = configuration_provider.get_value("content_template")
    LOGGER.debug("Content template %s", content_template)
    if content_template is None:
        raise ValueError("content_template is not set")
    ai_provider = configuration_provider.get_value("ai_provider")
    LOGGER.debug("AI provider %s", ai_provider)
    if ai_provider is None:
        raise ValueError("ai_provider is not set")
    ai_model = configuration_provider.get_value("ai_model")
    LOGGER.debug("AI model %s", ai_model)
    if ai_model is None:
        raise ValueError("ai_model is not set")
    ai_api_key = configuration_provider.get_secret("ai_api_key")
    if ai_api_key is None:
        raise ValueError("ai_api_key is not set")
    task_repository = DynamoTaskRepository(
        dynamo_client=create_boto3_client("dynamodb"),
        table_name=task_table_name,
    )

    model_factory = LangchainAgentModelFactory(
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_api_key=ai_api_key,
    )
    tools_factory = AsyncMCPToolsFactory(
        mcp_client=MultiServerMCPClient(
            {
                "titvo-mcp-server": {
                    "transport": "streamable_http",
                    "url": mcp_server_url,
                },
            }
        ),
    )
    langfuse_public_key = configuration_provider.get_secret("langfuse_public_key")
    langfuse_secret_key = configuration_provider.get_secret("langfuse_secret_key")
    langfuse_host = configuration_provider.get_value("langfuse_host")
    langfuse_callback_handler: Optional[CallbackHandler] = None
    langfuse_metadata: Optional[Dict[str, Any]] = None
    if (
        langfuse_public_key is not None
        and langfuse_secret_key is not None
        and langfuse_host is not None
    ):
        Langfuse(
            public_key=langfuse_public_key,
            secret_key=langfuse_secret_key,
            host=langfuse_host,
        )
        langfuse_callback_handler = CallbackHandler()
        langfuse_metadata = {
            "langfuse_session_id": task_id,
        }
    agent = LangchainAgent(
        system_prompt=system_prompt,
        model_factory=model_factory,
        tools_factory=tools_factory,
        langfuse_callback_handler=langfuse_callback_handler,
        langfuse_metadata=langfuse_metadata,
    )
    notification_service = NotificationService(
        bitbucket_repository=LambdaBitbucketRepository(
            function_name=bitbucket_repository_function_name,
        ),
        github_repository=LambdaGitHubRepository(
            function_name=github_repository_function_name,
        ),
        report_repository=LambdaReportRepository(
            function_name=report_repository_function_name,
        ),
    )
    analyse_code_use_case = AnalyseCodeUseCase(
        task_repository=task_repository,
        agent=agent,
        content_template=content_template,
        notification_service=notification_service,
    )
    await analyse_code_use_case.execute(task_id)


if __name__ == "__main__":
    asyncio.run(main())

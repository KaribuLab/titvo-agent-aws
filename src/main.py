import asyncio
import logging
import os
from logging.config import dictConfig
from typing import Any

import boto3
from langchain_mcp_adapters.client import MultiServerMCPClient

from code_analysis.application.analyse_code_use_case import AnalyseCodeUseCase
from code_analysis.infra.adapters.dynamo_task_repository import DynamoTaskRepository
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
    task_table_name = os.getenv("TASK_TABLE_NAME")
    LOGGER.debug("Task table name %s", task_table_name)
    if task_table_name is None:
        raise ValueError("TASK_TABLE_NAME is not set")
    config_table_name = os.getenv("CONFIG_TABLE_NAME")
    LOGGER.debug("Config table name %s", config_table_name)
    if config_table_name is None:
        raise ValueError("CONFIG_TABLE_NAME is not set")
    encryption_key_name = os.getenv("ENCRYPTION_KEY_NAME")
    LOGGER.debug("Encryption key name %s", encryption_key_name)
    if encryption_key_name is None:
        raise ValueError("ENCRYPTION_KEY_NAME is not set")
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
    ia_provider = configuration_provider.get_value("ia_provider")
    LOGGER.debug("IA provider %s", ia_provider)
    if ia_provider is None:
        raise ValueError("ia_provider is not set")
    ia_model = configuration_provider.get_value("ia_model")
    LOGGER.debug("IA model %s", ia_model)
    if ia_model is None:
        raise ValueError("ia_model is not set")
    ia_api_key = configuration_provider.get_secret("ia_api_key")
    LOGGER.debug("IA API key %s", ia_api_key)
    if ia_api_key is None:
        raise ValueError("ia_api_key is not set")
    task_repository = DynamoTaskRepository(
        dynamo_client=create_boto3_client("dynamodb"),
        table_name=task_table_name,
    )

    model_factory = LangchainAgentModelFactory(
        ia_provider=ia_provider,
        ia_model=ia_model,
        ia_api_key=ia_api_key,
    )
    tools_factory = AsyncMCPToolsFactory(
        mcp_client=MultiServerMCPClient({
            "titvo-mcp-server": {
                "transport": "streamable_http",
                "url": mcp_server_url,
            },
        }),
    )
    agent = LangchainAgent(
        system_prompt=system_prompt,
        model_factory=model_factory,
        tools_factory=tools_factory,
    )
    analyse_code_use_case = AnalyseCodeUseCase(
        task_repository=task_repository,
        agent=agent,
        content_template=content_template,
    )
    await analyse_code_use_case.execute(task_id)


if __name__ == "__main__":
    asyncio.run(main())

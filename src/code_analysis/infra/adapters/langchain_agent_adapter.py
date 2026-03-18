import logging
from enum import Enum
from typing import List

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from code_analysis.domain.ports.ai_agent import (
    AbstractAgent,
    AgentMessage,
    AgentModelFactory,
    AgentResponse,
    AgentToolsFactory,
    AsyncAgentToolsFactory,
)

LOGGER = logging.getLogger(__name__)


class AIProvider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"

    @classmethod
    def from_string(cls, ai_provider: str) -> "AIProvider":
        if ai_provider == "openai":
            return cls.OPENAI
        elif ai_provider == "anthropic":
            return cls.ANTHROPIC
        elif ai_provider == "google":
            return cls.GOOGLE
        else:
            raise ValueError(f"Invalid AI provider: {ai_provider}")


class AsyncMCPToolsFactory(AsyncAgentToolsFactory[BaseTool]):
    """Factory asíncrono - inicializa tools desde MCP client"""

    def __init__(self, mcp_client: MultiServerMCPClient):
        self._mcp_client = mcp_client

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """
        Sanitiza el nombre de la herramienta para que cumpla con el patrón de OpenAI.
        Solo permite: letras, números, guiones bajos y guiones.
        Reemplaza cualquier otro caracter con guión bajo.
        """
        import re

        # Reemplazar caracteres no permitidos con guión bajo
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        # Eliminar guiones bajos consecutivos
        sanitized = re.sub(r"_+", "_", sanitized)
        # Eliminar guiones bajos al inicio y final
        sanitized = sanitized.strip("_")
        return sanitized

    async def create_tools(self) -> List[BaseTool]:
        tools = await self._mcp_client.get_tools()
        # Sanitizar los nombres de las herramientas
        for tool in tools:
            tool.name = self._sanitize_tool_name(tool.name)
        return tools


class LangchainAgentModelFactory(AgentModelFactory[BaseChatModel]):
    def __init__(self, ai_provider: str, ai_model: str, ai_api_key: str):
        self._ai_provider = ai_provider
        self._ai_model = ai_model
        self._ai_api_key = ai_api_key

    def create_model(self) -> BaseChatModel:
        provider = AIProvider.from_string(self._ai_provider)
        if provider == AIProvider.OPENAI:
            return ChatOpenAI(model=self._ai_model, api_key=self._ai_api_key)
        elif provider == AIProvider.ANTHROPIC:
            return ChatAnthropic(model=self._ai_model, api_key=self._ai_api_key)
        elif provider == AIProvider.GOOGLE:
            return ChatGoogleGenerativeAI(
                model=self._ai_model, api_key=self._ai_api_key
            )


class LangchainAgent(AbstractAgent[BaseTool, BaseChatModel]):
    def __init__(
        self,
        system_prompt: str,
        model_factory: AgentModelFactory[BaseChatModel],
        tools_factory: AgentToolsFactory[BaseTool] | AsyncAgentToolsFactory[BaseTool],
    ):
        super().__init__(system_prompt, model_factory, tools_factory)
        self.__agent = None

    async def _initialize(self, model: BaseChatModel, tools: List[BaseTool]) -> None:
        if self.__agent is None:
            self.__agent = create_agent(
                system_prompt=self._system_prompt,
                model=model,
                tools=tools,
            )

    async def _invoke_wrapped(
        self, message: AgentMessage, temperature: float = 0.0
    ) -> AgentResponse:
        response = await self.__agent.ainvoke(
            {
                "messages": [
                    {"role": message.role, "content": message.content},
                ]
            },
            config={"temperature": temperature, "recursion_limit": 100},
        )
        LOGGER.debug("Response from agent: %s", response)

        # Obtener el último mensaje de la lista de mensajes
        last_message = response["messages"][-1]

        return AgentResponse(
            content=last_message.content,
            metadata={
                "usage_metadata": last_message.usage_metadata,
            },
        )

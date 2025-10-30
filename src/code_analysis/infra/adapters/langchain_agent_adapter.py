from enum import Enum
from typing import List

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from code_analysis.domain.ports.ia_agent import (
    AbstractAgent,
    AgentMessage,
    AgentModelFactory,
    AgentResponse,
    AgentToolsFactory,
    AsyncAgentToolsFactory,
)


class IAProvider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"

    @classmethod
    def from_string(cls, ia_provider: str) -> "IAProvider":
        if ia_provider == "openai":
            return cls.OPENAI
        elif ia_provider == "anthropic":
            return cls.ANTHROPIC
        elif ia_provider == "google":
            return cls.GOOGLE
        else:
            raise ValueError(f"Invalid IA provider: {ia_provider}")


class AsyncMCPToolsFactory(AsyncAgentToolsFactory[BaseTool]):
    """Factory asíncrono - inicializa tools desde MCP client"""

    def __init__(self, mcp_client: MultiServerMCPClient):
        self._mcp_client = mcp_client

    async def create_tools(self) -> List[BaseTool]:
        return await self._mcp_client.get_tools()


class LangchainAgentModelFactory(AgentModelFactory[BaseChatModel]):
    def __init__(self, ia_provider: str, ia_model: str, ia_api_key: str):
        self._ia_provider = ia_provider
        self._ia_model = ia_model
        self._ia_api_key = ia_api_key

    def create_model(self) -> BaseChatModel:
        provider = IAProvider.from_string(self._ia_provider)
        if provider == IAProvider.OPENAI:
            return ChatOpenAI(model=self._ia_model, api_key=self._ia_api_key)
        elif provider == IAProvider.ANTHROPIC:
            return ChatAnthropic(model=self._ia_model, api_key=self._ia_api_key)
        elif provider == IAProvider.GOOGLE:
            return ChatGoogleGenerativeAI(
                model=self._ia_model, api_key=self._ia_api_key
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
            {"messages": [{"role": message.role, "content": message.content}]},
            config={"temperature": temperature},
        )
        return AgentResponse(
            content=response.content,
            metadata={
                "usage_metadata": response.usage_metadata,
            },
        )

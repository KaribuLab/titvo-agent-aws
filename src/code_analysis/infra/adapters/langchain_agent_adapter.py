import logging
from enum import Enum
from typing import Any, Dict, List, Optional

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langfuse.langchain import CallbackHandler

from code_analysis.domain.ports.ia_agent import (
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
    OPENROUTER = "openrouter"

    @classmethod
    def from_string(cls, ai_provider: str) -> "AIProvider":
        if ai_provider == "openai":
            return cls.OPENAI
        elif ai_provider == "anthropic":
            return cls.ANTHROPIC
        elif ai_provider == "google":
            return cls.GOOGLE
        elif ai_provider == "openrouter":
            return cls.OPENROUTER
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
    def __init__(
        self,
        ai_provider: str,
        ai_model: str,
        ai_api_key: str,
        ai_base_url: str | None = None,
    ):
        self._ai_provider = ai_provider
        self._ai_model = ai_model
        self._ai_api_key = ai_api_key
        self._ai_base_url = ai_base_url

    def create_model(self) -> BaseChatModel:
        provider = AIProvider.from_string(self._ai_provider)
        # Treat an empty string the same as unset, and only ever forward
        # https:// endpoints: ai_base_url ends up carrying ai_api_key in the
        # Authorization header, so a plain-http or malformed value would leak
        # the provider credentials to whatever host it points at.
        base_url = self._ai_base_url or None
        if base_url is not None and not base_url.startswith("https://"):
            raise ValueError(f"ai_base_url must use https://, got: {base_url}")
        LOGGER.info(
            "Using provider=%s, model=%s, base_url=%s",
            provider.value,
            self._ai_model,
            base_url,
        )
        # Requested with temperature=0 so expert/consolidation analysis is
        # deterministic. Some providers (e.g. OpenAI reasoning models like
        # gpt-5-non-chat) silently ignore or override this at construction
        # time, so the *effective* value is re-read from the model below and
        # logged rather than assumed.
        if provider == AIProvider.OPENAI:
            # Custom OpenAI-compatible endpoints expose the Chat Completions
            # API, not the Responses API, so use_responses_api must be False.
            if base_url is not None:
                model = ChatOpenAI(
                    model=self._ai_model,
                    api_key=self._ai_api_key,
                    base_url=base_url,
                    use_responses_api=False,
                    temperature=0,
                )
            else:
                model = ChatOpenAI(
                    model=self._ai_model,
                    api_key=self._ai_api_key,
                    use_responses_api=True,
                    temperature=0,
                )
        elif provider == AIProvider.OPENROUTER:
            # OpenRouter is OpenAI-compatible but only supports Chat Completions,
            # never the Responses API.
            model = ChatOpenAI(
                model=self._ai_model,
                api_key=self._ai_api_key,
                base_url=base_url or "https://openrouter.ai/api/v1",
                use_responses_api=False,
                temperature=0,
            )
        elif provider == AIProvider.ANTHROPIC:
            if base_url is not None:
                model = ChatAnthropic(
                    model=self._ai_model,
                    api_key=self._ai_api_key,
                    base_url=base_url,
                    temperature=0,
                )
            else:
                model = ChatAnthropic(
                    model=self._ai_model, api_key=self._ai_api_key, temperature=0
                )
        elif provider == AIProvider.GOOGLE:
            if base_url is not None:
                model = ChatGoogleGenerativeAI(
                    model=self._ai_model,
                    api_key=self._ai_api_key,
                    client_options={"api_endpoint": base_url},
                    temperature=0,
                )
            else:
                model = ChatGoogleGenerativeAI(
                    model=self._ai_model, api_key=self._ai_api_key, temperature=0
                )

        effective_temperature = getattr(model, "temperature", None)
        if effective_temperature == 0:
            LOGGER.info(
                "Model temperature=0 (deterministic sampling confirmed)",
            )
        else:
            LOGGER.warning(
                "Model %s did not accept temperature=0 (effective=%s); "
                "reproducibility across scans is not guaranteed for this model",
                self._ai_model,
                effective_temperature,
            )
        return model


class LangchainAgent(AbstractAgent[BaseTool, BaseChatModel]):
    def __init__(
        self,
        system_prompt: str,
        model_factory: AgentModelFactory[BaseChatModel],
        tools_factory: AgentToolsFactory[BaseTool] | AsyncAgentToolsFactory[BaseTool],
        langfuse_callback_handler: Optional[CallbackHandler],
        langfuse_metadata: Optional[Dict[str, Any]],
    ):
        super().__init__(system_prompt, model_factory, tools_factory)
        self.__agent = None
        self.__langfuse_callback_handler = langfuse_callback_handler
        self.__langfuse_metadata = langfuse_metadata

    async def _initialize(self, model: BaseChatModel, tools: List[BaseTool]) -> None:
        if self.__agent is None:
            self.__agent = create_agent(
                system_prompt=self._system_prompt,
                model=model,
                tools=tools,
            )

    async def _invoke_wrapped(self, message: AgentMessage) -> AgentResponse:
        # Sampling temperature is fixed at model-construction time
        # (LangchainAgentModelFactory.create_model), not per-invocation.
        config = {"recursion_limit": 100}
        if (
            self.__langfuse_callback_handler is not None
            and self.__langfuse_metadata is not None
        ):
            config["callbacks"] = [self.__langfuse_callback_handler]
            config["metadata"] = self.__langfuse_metadata
        response = await self.__agent.ainvoke(
            {
                "messages": [
                    {"role": message.role, "content": message.content},
                ]
            },
            config=config,
        )
        LOGGER.debug("Response from agent: %s", response)

        # Obtener el último mensaje de la lista de mensajes
        last_message = response["messages"][-1]

        # Manejar content como string o lista (Responses API devuelve lista)
        content = last_message.content
        if isinstance(content, list):
            # Extraer texto de los bloques de contenido
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    text_parts.append(block["text"])
            content = "".join(text_parts)

        return AgentResponse(
            content=content,
            metadata={
                "usage_metadata": last_message.usage_metadata,
            },
        )

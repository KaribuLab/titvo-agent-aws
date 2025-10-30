from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union

T = TypeVar("T")
M = TypeVar("M")


@dataclass
class AgentMessage:
    role: str
    content: str


@dataclass
class AgentToolsFactory(ABC, Generic[T]):
    """Factory síncrono para herramientas (tools ya inicializadas)"""

    @abstractmethod
    def create_tools(self) -> List[T]:
        raise NotImplementedError


@dataclass
class AsyncAgentToolsFactory(ABC, Generic[T]):
    """Factory asíncrono para herramientas (requiere await)"""

    @abstractmethod
    async def create_tools(self) -> List[T]:
        raise NotImplementedError


class AgentModelFactory(ABC, Generic[M]):
    @abstractmethod
    def create_model(self) -> M:
        raise NotImplementedError


@dataclass
class AgentResponse:
    content: str
    metadata: Optional[Dict[str, Any]] = None


class AbstractAgent(ABC, Generic[T, M]):
    def __init__(
        self,
        system_prompt: str,
        model_factory: AgentModelFactory[M],
        tools_factory: Union[AgentToolsFactory[T], AsyncAgentToolsFactory[T]],
    ):
        self._system_prompt = system_prompt
        self._model_factory = model_factory
        self._tools_factory = tools_factory
        self._model = None
        self._tools = None

    @abstractmethod
    async def _initialize(self, model: M, tools: List[T]) -> None:
        raise NotImplementedError

    async def __ensure_initialized(self) -> None:
        if self._tools is None:
            if isinstance(self._tools_factory, AsyncAgentToolsFactory):
                self._tools = await self._tools_factory.create_tools()
            elif isinstance(self._tools_factory, AgentToolsFactory):
                self._tools = self._tools_factory.create_tools()
            else:
                raise ValueError(f"Invalid tools factory: {type(self._tools_factory)}")
        if self._model is None:
            self._model = self._model_factory.create_model()
        await self._initialize(self._model, self._tools)

    @abstractmethod
    def _invoke_wrapped(
        self, message: AgentMessage, temperature: float = 0.0
    ) -> AgentResponse:
        raise NotImplementedError

    async def invoke(
        self, message: AgentMessage, temperature: float = 0.0
    ) -> AgentMessage:
        await self.__ensure_initialized()
        response = await self._invoke_wrapped(message, temperature)
        return AgentResponse(
            content=response.content,
            metadata=response.metadata,
        )

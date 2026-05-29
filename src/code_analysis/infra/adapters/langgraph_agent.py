"""LangGraph-based agent implementation.

Implements AbstractAgent using LangGraph workflow with multiple expert nodes.
"""

import json
import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langfuse.langchain import CallbackHandler

from code_analysis.domain.ports.ia_agent import (
    AbstractAgent,
    AgentMessage,
    AgentModelFactory,
    AgentResponse,
    AsyncAgentToolsFactory,
)
from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
    RagRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState
from code_analysis.infra.adapters.langgraph.workflow import create_workflow

LOGGER = logging.getLogger(__name__)


class LangGraphAgent(AbstractAgent):
    """Agent implementation using LangGraph with expert nodes.

    This agent uses a StateGraph workflow with:
    - MCP Retrieval Node (fetches files from git)
    - 5 Expert Nodes (prompt_hardening, owasp_api, owasp_web, devsecops, code_vulns)
    - Merge Node (deduplication, final status)
    """

    def __init__(
        self,
        system_prompt: str,
        model_factory: AgentModelFactory[BaseChatModel],
        tools_factory: AsyncAgentToolsFactory,
        langfuse_callback_handler: CallbackHandler | None = None,
        langfuse_metadata: dict[str, Any] | None = None,
        rag_node: RagRetrievalNode | None = None,
    ):
        super().__init__(system_prompt, model_factory, tools_factory)
        self._langfuse_handler = langfuse_callback_handler
        self._langfuse_metadata = langfuse_metadata or {}
        self._rag_node = rag_node
        self._workflow = None
        self._mcp_client = None

    async def _initialize(
        self,
        model: BaseChatModel,
        tools: list[Any],
    ) -> None:
        """Initialize the LangGraph workflow.

        Note: tools parameter is not used directly as MCP client
        handles tool invocation internally.
        """
        if self._workflow is not None:
            return

        LOGGER.info("Initializing LangGraph workflow")

        # Extract MCP client from tools factory
        # The AsyncMCPToolsFactory has the client
        if hasattr(self._tools_factory, "_mcp_client"):
            self._mcp_client = self._tools_factory._mcp_client
        else:
            # Create new client if not available
            from langchain_mcp_adapters.client import MultiServerMCPClient

            self._mcp_client = MultiServerMCPClient(
                {
                    "titvo-mcp-server": {
                        "transport": "streamable_http",
                        "url": "http://localhost:3000/mcp",  # Default, override in invoke
                    }
                }
            )

        # Build workflow
        self._workflow = create_workflow(
            self._mcp_client, model, rag_node=self._rag_node
        )
        LOGGER.info("LangGraph workflow initialized")

    async def _invoke_wrapped(
        self,
        message: AgentMessage,
        temperature: float = 0.0,
    ) -> AgentResponse:
        """Execute the LangGraph workflow.

        Args:
            message: Contains task parameters in content
            temperature: Ignored (experts use deterministic analysis)

        Returns:
            AgentResponse with JSON result
        """
        if self._workflow is None:
            raise RuntimeError("Agent not initialized. Call invoke() first.")

        try:
            # Parse message content for task parameters
            params = self._parse_message_content(message.content)

            LOGGER.info(
                "[LangGraphAgent] Starting analysis for %s @ %s",
                params.get("repository_url", "unknown"),
                params.get("commit_hash", "unknown")[:8],
            )

            # Prepare initial state
            initial_state: AgentState = {
                "task_id": params.get("task_id", "unknown"),
                "repository_url": params.get("repository_url", ""),
                "branch": params.get("branch", ""),
                "commit_hash": params.get("commit_hash", ""),
                "extra_args": params.get("extra_args", {}),
                "files": [],
                "scaned_files": 0,
                "issues": [],
                "current_expert_index": 0,
                "expert_errors": [],
            }

            # Execute workflow with optional Langfuse tracing
            config = {"recursion_limit": 100}
            if self._langfuse_handler:
                config["callbacks"] = [self._langfuse_handler]
                config["metadata"] = {
                    **self._langfuse_metadata,
                    "agent_type": "langgraph",
                    "repository_url": initial_state["repository_url"],
                }

            LOGGER.info(
                "[LangGraphAgent] Invoking workflow: task_id=%s, repo=%s",
                initial_state["task_id"],
                initial_state["repository_url"],
            )
            result = await self._workflow.ainvoke(initial_state, config=config)
            LOGGER.info(
                "[LangGraphAgent] Workflow completed, keys: %s",
                list(result.keys()),
            )

            # Extract final output
            final_output = result.get("final_output", {})
            if not final_output:
                # Fallback: construct from state
                final_output = {
                    "status": result.get("status", "FAILED"),
                    "scaned_files": result.get("scaned_files", 0),
                    "issues": [issue.to_dict() for issue in result.get("issues", [])],
                }

            LOGGER.info(
                "LangGraph workflow completed: status=%s, issues=%d",
                final_output.get("status", "UNKNOWN"),
                len(final_output.get("issues", [])),
            )

            return AgentResponse(
                content=json.dumps(final_output),
                metadata={
                    "status": final_output.get("status"),
                    "scaned_files": final_output.get("scaned_files"),
                    "issue_count": len(final_output.get("issues", [])),
                    "expert_errors": result.get("expert_errors", []),
                },
            )

        except Exception as e:
            LOGGER.exception("LangGraph workflow failed")
            error_result = {
                "status": "FAILED",
                "scaned_files": 0,
                "issues": [],
                "error": str(e),
            }
            return AgentResponse(
                content=json.dumps(error_result),
                metadata={"error": str(e)},
            )

    def _parse_message_content(self, content: str) -> dict[str, Any]:
        """Parse message content for task parameters.

        Expects format from content_template:
        Repository: {url}
        Commit: {hash}
        """
        params: dict[str, Any] = {
            "repository_url": "",
            "branch": "",
            "commit_hash": "",
            "extra_args": {},
        }

        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("Repository:"):
                params["repository_url"] = line.replace("Repository:", "").strip()
            elif line.startswith("Branch:"):
                params["branch"] = line.replace("Branch:", "").strip()
            elif line.startswith("Commit:"):
                params["commit_hash"] = line.replace("Commit:", "").strip()

        return params

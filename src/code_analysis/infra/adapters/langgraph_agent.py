"""LangGraph-based agent implementation.

Implements AbstractAgent using LangGraph workflow with multiple expert nodes.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langfuse.langchain import CallbackHandler

from code_analysis import prompts as prompt_registry
from code_analysis.domain.ports.ia_agent import (
    AbstractAgent,
    AgentMessage,
    AgentModelFactory,
    AgentResponse,
    AsyncAgentToolsFactory,
)
from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ExpertRuntimeConfig,
)
from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
    RagRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState
from code_analysis.infra.adapters.langgraph.workflow import create_workflow
from code_analysis.infra.adapters.model_usage import UsageModel

LOGGER = logging.getLogger(__name__)


class LangGraphAgent(AbstractAgent):
    """Agent implementation using LangGraph with expert nodes.

    This agent uses a StateGraph workflow with:
    - MCP Retrieval Node (fetches files from git)
    - Classify Runtime Node (deterministic runtime labels per file)
    - 6 Expert Nodes running in parallel (batched, chunked, retried)
    - Merge Node (two-level consolidation, incomplete reporting, final status)
    """

    def __init__(
        self,
        model_factory: AgentModelFactory[BaseChatModel],
        tools_factory: AsyncAgentToolsFactory,
        langfuse_callback_handler: CallbackHandler | None = None,
        langfuse_metadata: dict[str, Any] | None = None,
        rag_node: RagRetrievalNode | None = None,
        expert_config: ExpertRuntimeConfig | None = None,
        retrieval_node: Any | None = None,
        usage_provider: str | None = None,
        usage_model: str | None = None,
        usage_base_url: str | None = None,
    ):
        # Experts compose their own system message (common preamble + domain
        # prompt via PromptRegistry); the base-class slot keeps the preamble
        # only for introspection.
        super().__init__(
            prompt_registry.get_common_preamble(), model_factory, tools_factory
        )
        self._langfuse_handler = langfuse_callback_handler
        self._langfuse_metadata = langfuse_metadata or {}
        self._rag_node = rag_node
        self._expert_config = expert_config or ExpertRuntimeConfig()
        self._retrieval_node = retrieval_node
        self._usage_settings = (usage_provider, usage_model, usage_base_url)
        self._usage = None
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
        if self._retrieval_node is not None:
            self._mcp_client = None
        elif hasattr(self._tools_factory, "_mcp_client"):
            self._mcp_client = self._tools_factory._mcp_client
        else:
            # Create new client if not available
            from langchain_mcp_adapters.client import MultiServerMCPClient

            self._mcp_client = MultiServerMCPClient(
                {
                    "titvo-mcp-server": {
                        "transport": "streamable_http",
                        "url": "http://localhost:3000/mcp",
                    }
                }
            )

        # AWS and lab use the same accounting boundary around all model calls.
        provider, model_name, base_url = self._usage_settings
        if provider is not None:
            self._usage = UsageModel(model, "real", provider, model_name, base_url)
            model = self._usage
        # Build workflow
        self._workflow = create_workflow(
            self._mcp_client,
            model,
            rag_node=self._rag_node,
            expert_config=self._expert_config,
            retrieval_node=self._retrieval_node,
        )
        LOGGER.info("LangGraph workflow initialized")

    async def _invoke_wrapped(
        self,
        message: AgentMessage,
    ) -> AgentResponse:
        """Execute the LangGraph workflow.

        Args:
            message: Contains task parameters in content

        Returns:
            AgentResponse with JSON result

        Note:
            Deterministic sampling (temperature=0) is enforced when the model
            is built in LangchainAgentModelFactory.create_model(), not here.
        """
        if self._workflow is None:
            raise RuntimeError("Agent not initialized. Call invoke() first.")

        started = time.monotonic()
        started_at = datetime.now(timezone.utc)
        try:
            # Parse message content for task parameters, then overlay structured
            # metadata from the use case. Operational values must not depend only
            # on prompt parsing.
            params = self._parse_message_content(message.content)
            if message.metadata:
                params.update(
                    {k: v for k, v in message.metadata.items() if v is not None}
                )

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
                "scan_mode": params.get("scan_mode", "commit"),
                "scan_ref": params.get("scan_ref", params.get("branch", "")),
                "files": [],
                "scaned_files": 0,
                "issues": [],
                "current_expert_index": 0,
                "expert_errors": [],
                "failed_batches": [],
                "expert_metadata": {},
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
                LOGGER.warning(
                    "[LangGraphAgent] final_output missing from workflow result; "
                    "falling back to state.issues. result_keys=%s",
                    list(result.keys()),
                )
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

            final_output["metrics"] = self._execution_metrics(
                final_output, started, started_at, message.metadata
            )
            if self._usage is not None:
                final_output["usage"] = self._usage.summary()
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
                "coverage": {"complete": False, "errors": [str(e)]},
            }
            error_result["metrics"] = self._execution_metrics(
                error_result, started, started_at, message.metadata
            )
            if self._usage is not None:
                error_result["usage"] = self._usage.summary()
            return AgentResponse(
                content=json.dumps(error_result),
                metadata={"error": str(e)},
            )

    @staticmethod
    def _execution_metrics(result, started, started_at, metadata):
        """Persist measured duration and batches without fabricating historical data."""
        finished = datetime.now(timezone.utc)
        experts = result.get("coverage", {}).get("experts", {})
        metrics = {
            "started_at": started_at.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "total_batches": sum(
                expert.get("batches_total", expert.get("batches", 0))
                for expert in experts.values()
            ),
            "completed_batches": sum(
                expert.get("batches_completed", 0) for expert in experts.values()
            ),
        }
        created = (metadata or {}).get("created_at")
        if isinstance(created, str):
            try:
                created_at = datetime.fromisoformat(created)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                metrics["task_duration_seconds"] = max(
                    0.0, round((finished - created_at).total_seconds(), 3)
                )
            except ValueError:
                pass
        return metrics

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

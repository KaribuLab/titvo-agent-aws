"""LangGraph workflow builder for security analysis."""

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, StateGraph

from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    create_expert_nodes,
)
from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)


class LangGraphWorkflowBuilder:
    """Builder for the security analysis LangGraph workflow.

    Constructs a StateGraph with:
    1. MCP Retrieval Node (fetches files)
    2. Sequential Expert Nodes (5 experts)
    3. Merge Findings Node (deduplication, status)
    """

    def __init__(
        self,
        mcp_client: MultiServerMCPClient,
        model: BaseChatModel,
    ):
        self._mcp_client = mcp_client
        self._model = model

    def build(self) -> StateGraph:
        """Build and return the configured StateGraph."""
        LOGGER.info("Building LangGraph workflow")

        # Create nodes
        mcp_node = MCPRetrievalNode(self._mcp_client)
        expert_nodes = create_expert_nodes(self._model)
        merge_node = MergeFindingsNode()

        # Build graph
        workflow = StateGraph(AgentState)

        # Add MCP retrieval node
        workflow.add_node("mcp_retrieve", mcp_node)

        # Add expert nodes
        for expert in expert_nodes:
            workflow.add_node(f"expert_{expert.expert_name}", expert)

        # Add merge node
        workflow.add_node("merge", merge_node)

        # Set entry point
        workflow.set_entry_point("mcp_retrieve")

        # Define routing from MCP
        def route_from_mcp(state: AgentState) -> str:
            """Route based on MCP result."""
            if state.get("mcp_error"):
                return "merge"  # Skip experts on error
            if not state.get("files"):
                return "merge"  # Skip experts if no files
            return "expert_prompt_hardening"

        workflow.add_conditional_edges(
            "mcp_retrieve",
            route_from_mcp,
            {
                "expert_prompt_hardening": "expert_prompt_hardening",
                "merge": "merge",
            },
        )

        # Chain experts sequentially
        expert_names = [f"expert_{e.expert_name}" for e in expert_nodes]

        for i in range(len(expert_names) - 1):
            current = expert_names[i]
            next_node = expert_names[i + 1]
            workflow.add_edge(current, next_node)
            LOGGER.debug("Connected %s -> %s", current, next_node)

        # Connect last expert to merge
        workflow.add_edge(expert_names[-1], "merge")

        # Connect merge to end
        workflow.add_edge("merge", END)

        LOGGER.info(
            "[WorkflowBuilder] Workflow built: entry=mcp_retrieve, "
            "%d experts, merge_node",
            len(expert_nodes),
        )

        compiled = workflow.compile()
        LOGGER.info("[WorkflowBuilder] Workflow compiled successfully")
        return compiled

    def build_with_error_handling(self) -> StateGraph:
        """Build workflow with comprehensive error handling."""
        try:
            return self.build()
        except Exception as e:
            LOGGER.exception("Failed to build workflow")
            raise WorkflowBuildError(f"Failed to build LangGraph workflow: {e}") from e


class WorkflowBuildError(Exception):
    """Error when building the LangGraph workflow."""
    pass


def create_workflow(
    mcp_client: MultiServerMCPClient,
    model: BaseChatModel,
) -> Any:
    """Factory function to create compiled workflow."""
    builder = LangGraphWorkflowBuilder(mcp_client, model)
    return builder.build()

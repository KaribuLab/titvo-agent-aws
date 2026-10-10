"""LangGraph workflow builder for security analysis."""

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, StateGraph

from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ExpertRuntimeConfig,
)
from code_analysis.infra.adapters.langgraph.nodes.classify_runtime_node import (
    ClassifyRuntimeNode,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    EXPERT_RUNTIMES,
    create_expert_nodes,
)
from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
    MCPRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)
from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
    RagRetrievalNode,
)
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

CLASSIFY_NODE = "classify_runtime"
MERGE_NODE = "merge"


class LangGraphWorkflowBuilder:
    """Builder for the security analysis LangGraph workflow.

    Topology::

        mcp_retrieve → [rag_retrieve] → classify_runtime → expert_* (×6, parallel)
                                                                      ↓
        mcp_retrieve ─(error / no files)────────────────────────→ merge → END
    """

    def __init__(
        self,
        mcp_client: MultiServerMCPClient,
        model: BaseChatModel,
        rag_node: RagRetrievalNode | None = None,
        expert_config: ExpertRuntimeConfig | None = None,
        retrieval_node: Any | None = None,
    ):
        self._mcp_client = mcp_client
        self._model = model
        self._rag_node = rag_node
        self._expert_config = expert_config or ExpertRuntimeConfig.from_env()
        self._retrieval_node = retrieval_node

    def build(self) -> StateGraph:
        """Build and return the compiled StateGraph."""
        LOGGER.info("Building LangGraph workflow")

        mcp_node = self._retrieval_node or MCPRetrievalNode(self._mcp_client)
        classify_node = ClassifyRuntimeNode(EXPERT_RUNTIMES)
        expert_nodes = create_expert_nodes(self._model, self._expert_config)
        merge_node = MergeFindingsNode(self._model)

        workflow = StateGraph(AgentState)
        workflow.add_node("mcp_retrieve", mcp_node)
        if self._rag_node is not None:
            workflow.add_node("rag_retrieve", self._rag_node)
        workflow.add_node(CLASSIFY_NODE, classify_node)
        for expert in expert_nodes:
            workflow.add_node(f"expert_{expert.expert_name}", expert)
        workflow.add_node(MERGE_NODE, merge_node)

        workflow.set_entry_point("mcp_retrieve")

        after_mcp = "rag_retrieve" if self._rag_node is not None else CLASSIFY_NODE

        def route_from_mcp(state: AgentState) -> str:
            if state.get("mcp_error"):
                return MERGE_NODE
            if not state.get("files"):
                return MERGE_NODE
            return after_mcp

        workflow.add_conditional_edges(
            "mcp_retrieve",
            route_from_mcp,
            {after_mcp: after_mcp, MERGE_NODE: MERGE_NODE},
        )
        if self._rag_node is not None:
            workflow.add_edge("rag_retrieve", CLASSIFY_NODE)

        # Fan-out: classification → every expert; fan-in: every expert → merge.
        for expert in expert_nodes:
            name = f"expert_{expert.expert_name}"
            workflow.add_edge(CLASSIFY_NODE, name)
            workflow.add_edge(name, MERGE_NODE)

        workflow.add_edge(MERGE_NODE, END)

        LOGGER.info(
            "[WorkflowBuilder] Workflow built: entry=mcp_retrieve, classify, "
            "%d parallel experts, merge; expert_config=%s",
            len(expert_nodes),
            self._expert_config.to_dict(),
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


def create_workflow(
    mcp_client: MultiServerMCPClient,
    model: BaseChatModel,
    rag_node: RagRetrievalNode | None = None,
    expert_config: ExpertRuntimeConfig | None = None,
    retrieval_node: Any | None = None,
) -> Any:
    """Factory function to create compiled workflow."""
    builder = LangGraphWorkflowBuilder(
        mcp_client,
        model,
        rag_node=rag_node,
        expert_config=expert_config,
        retrieval_node=retrieval_node,
    )
    return builder.build()

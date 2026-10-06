"""Concrete expert node implementations for LangGraph workflow.

Experts select files by runtime (see ``runtime_classifier``), never by file
name patterns, and there is no fallback to "all files": an expert with no
matching files is skipped.
"""

from langchain_core.language_models.chat_models import BaseChatModel

from code_analysis.domain.services.runtime_classifier import Runtime
from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ALL_RUNTIME_VALUES,
    BaseExpertNode,
    ExpertRuntimeConfig,
)

_ALL = set(ALL_RUNTIME_VALUES)

EXPERT_RUNTIMES: dict[str, set[str]] = {
    "prompt_hardening": set(_ALL),
    "owasp_api": {
        Runtime.SERVER.value,
        Runtime.BROWSER.value,
        Runtime.UNKNOWN.value,
        Runtime.CONFIG.value,
    },
    "owasp_web": {
        Runtime.BROWSER.value,
        Runtime.SERVER.value,
        Runtime.UNKNOWN.value,
    },
    "owasp_mobile": {Runtime.MOBILE.value},
    "devsecops": {
        Runtime.INFRA.value,
        Runtime.CONFIG.value,
        Runtime.TEST.value,
    },
    "code_vulnerabilities": set(_ALL),
}


class PromptHardeningNode(BaseExpertNode):
    """Expert node for detecting prompt injection attempts in code."""

    @property
    def expert_name(self) -> str:
        return "prompt_hardening"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


class OwaspApiNode(BaseExpertNode):
    """Expert node for OWASP API Security Top 10 analysis.

    Includes browser code: frontend HTTP clients are direct evidence of how
    the API is authenticated.
    """

    @property
    def expert_name(self) -> str:
        return "owasp_api"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


class OwaspWebNode(BaseExpertNode):
    """Expert node for OWASP Web Top 10 analysis."""

    @property
    def expert_name(self) -> str:
        return "owasp_web"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


class OwaspMobileNode(BaseExpertNode):
    """Expert node for OWASP Mobile security analysis."""

    @property
    def expert_name(self) -> str:
        return "owasp_mobile"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


class DevSecOpsNode(BaseExpertNode):
    """Expert node for CI/CD, IaC, container and secret-management security.

    Test files belong here: they are not part of the front/back attack
    surface but routinely leak secrets and insecure configuration.
    """

    @property
    def expert_name(self) -> str:
        return "devsecops"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


class CodeVulnerabilitiesNode(BaseExpertNode):
    """Expert node for language-level code vulnerabilities."""

    @property
    def expert_name(self) -> str:
        return "code_vulnerabilities"

    def get_runtimes(self) -> set[str]:
        return set(EXPERT_RUNTIMES[self.expert_name])


# Expert registry for convenient access
EXPERT_CLASSES = {
    "prompt_hardening": PromptHardeningNode,
    "owasp_api": OwaspApiNode,
    "owasp_web": OwaspWebNode,
    "owasp_mobile": OwaspMobileNode,
    "devsecops": DevSecOpsNode,
    "code_vulnerabilities": CodeVulnerabilitiesNode,
}


def create_expert_nodes(
    model: BaseChatModel,
    config: ExpertRuntimeConfig | None = None,
) -> list[BaseExpertNode]:
    """Factory function to create all expert nodes sharing one config."""
    shared = config or ExpertRuntimeConfig()
    return [
        PromptHardeningNode(model, shared),
        OwaspApiNode(model, shared),
        OwaspWebNode(model, shared),
        OwaspMobileNode(model, shared),
        DevSecOpsNode(model, shared),
        CodeVulnerabilitiesNode(model, shared),
    ]

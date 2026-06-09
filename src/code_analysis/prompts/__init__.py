"""Prompt registry for loading prompts from package resources."""

import importlib.resources as resources
from functools import cache
from typing import Optional


class PromptRegistry:
    """Registry for loading prompts from package resources.

    All prompts are bundled with the agent package and loaded via
    importlib.resources. Changes to prompts require a new Docker build.
    """

    _instance: Optional["PromptRegistry"] = None

    def __new__(cls) -> "PromptRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @cache
    def get_system_prompt(self) -> str:
        """Load the main system prompt."""
        return resources.read_text("code_analysis.prompts", "system_prompt.md")

    @cache
    def get_content_template(self) -> str:
        """Load the content template for user messages."""
        return resources.read_text("code_analysis.prompts", "content_template.md")

    @cache
    def get_orchestrator_prompt(self) -> str:
        """Load the orchestrator node prompt for LangGraph."""
        return resources.read_text("code_analysis.prompts", "orchestrator_prompt.md")

    @cache
    def get_findings_consolidation_prompt(self) -> str:
        """Load the findings consolidation prompt."""
        return resources.read_text(
            "code_analysis.prompts",
            "findings_consolidation.md",
        )

    @cache
    def get_expert_prompt(self, expert_name: str) -> str:
        """Load an expert-specific prompt.

        Args:
            expert_name: One of: prompt_hardening, owasp_api, owasp_web,
                        owasp_mobile, devsecops, code_vulnerabilities

        Returns:
            The expert prompt content

        Raises:
            ValueError: If expert_name is not recognized
        """
        expert_files = {
            "prompt_hardening": "experts/prompt_hardening.md",
            "owasp_api": "experts/owasp_api.md",
            "owasp_web": "experts/owasp_web.md",
            "owasp_mobile": "experts/owasp_mobile.md",
            "devsecops": "experts/devsecops.md",
            "code_vulnerabilities": "experts/code_vulnerabilities.md",
        }

        if expert_name not in expert_files:
            raise ValueError(
                f"Unknown expert: {expert_name}. "
                f"Valid options: {list(expert_files.keys())}"
            )

        file_path = expert_files[expert_name]
        # Read from experts subdirectory
        return resources.read_text("code_analysis.prompts", file_path)

    def list_experts(self) -> list[str]:
        """Return list of available expert names."""
        return [
            "prompt_hardening",
            "owasp_api",
            "owasp_web",
            "owasp_mobile",
            "devsecops",
            "code_vulnerabilities",
        ]


# Global instance for convenience
_registry: Optional[PromptRegistry] = None


def get_registry() -> PromptRegistry:
    """Get the global PromptRegistry instance."""
    global _registry
    if _registry is None:
        _registry = PromptRegistry()
    return _registry


def get_system_prompt() -> str:
    """Load the main system prompt."""
    return get_registry().get_system_prompt()


def get_content_template() -> str:
    """Load the content template."""
    return get_registry().get_content_template()


def get_orchestrator_prompt() -> str:
    """Load the orchestrator prompt."""
    return get_registry().get_orchestrator_prompt()


def get_findings_consolidation_prompt() -> str:
    """Load the findings consolidation prompt."""
    return get_registry().get_findings_consolidation_prompt()


def get_expert_prompt(expert_name: str) -> str:
    """Load an expert-specific prompt."""
    return get_registry().get_expert_prompt(expert_name)


def list_experts() -> list[str]:
    """Return list of available expert names."""
    return get_registry().list_experts()

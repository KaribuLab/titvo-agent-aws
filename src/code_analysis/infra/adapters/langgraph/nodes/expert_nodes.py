"""Concrete expert node implementations for LangGraph workflow."""

from langchain_core.language_models.chat_models import BaseChatModel

from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    BaseExpertNode,
)


class PromptHardeningNode(BaseExpertNode):
    """Expert node for detecting prompt injection attempts in code."""

    @property
    def expert_name(self) -> str:
        return "prompt_hardening"

    def get_file_patterns(self) -> list[str]:
        """Analyze all files - prompt injection can be anywhere."""
        return []


class OwaspApiNode(BaseExpertNode):
    """Expert node for OWASP API Security Top 10 analysis."""

    @property
    def expert_name(self) -> str:
        return "owasp_api"

    def get_file_patterns(self) -> list[str]:
        """Focus on API-related files."""
        return [
            "*route*",
            "*api*",
            "*controller*",
            "*endpoint*",
            "*handler*",
            "openapi*",
            "swagger*",
        ]


class OwaspWebNode(BaseExpertNode):
    """Expert node for OWASP Web Top 10 analysis."""

    @property
    def expert_name(self) -> str:
        return "owasp_web"

    def get_file_patterns(self) -> list[str]:
        """Focus on web application files."""
        return [
            "*.html",
            "*.htm",
            "*template*",
            "*view*",
            "*frontend*",
            "*script*",
            "*xss*",
            "*csrf*",
            "*.js",
            "*.jsx",
            "*.ts",
            "*.tsx",
            "*.vue",
        ]


class OwaspMobileNode(BaseExpertNode):
    """Expert node for OWASP Mobile security analysis."""

    @property
    def expert_name(self) -> str:
        return "owasp_mobile"

    def get_file_patterns(self) -> list[str]:
        """Focus on Android, iOS, Flutter, and React Native files."""
        return [
            "*AndroidManifest.xml",
            "*network_security_config.xml",
            "*.kt",
            "*.kts",
            "*.java",
            "*build.gradle",
            "*settings.gradle",
            "*proguard-rules.pro",
            "*Info.plist",
            "*.entitlements",
            "*.swift",
            "*.m",
            "*.mm",
            "*Podfile",
            "*Package.swift",
            "*pubspec.yaml",
            "*.dart",
            "*app.json",
            "*app.config.*",
            "*metro.config.*",
            "*react-native.config.*",
            "*.tsx",
            "*.jsx",
        ]


class DevSecOpsNode(BaseExpertNode):
    """Expert node for CI/CD, IaC, and container security."""

    @property
    def expert_name(self) -> str:
        return "devsecops"

    def get_file_patterns(self) -> list[str]:
        """Focus on DevOps and infrastructure files."""
        return [
            "*.yml",
            "*.yaml",
            "Dockerfile*",
            "docker-compose*",
            "*.tf",
            "*.tfvars",
            "*.hcl",
            "Jenkinsfile*",
            ".github/**",
            ".gitlab-ci*",
            "cloudformation/**",
            "k8s/**",
            "kubernetes/**",
            "helm/**",
            "requirements*.txt",
            "package*.json",
            "pom.xml",
        ]


class CodeVulnerabilitiesNode(BaseExpertNode):
    """Expert node for language-level code vulnerabilities."""

    @property
    def expert_name(self) -> str:
        return "code_vulnerabilities"

    def get_file_patterns(self) -> list[str]:
        """Analyze all code files."""
        return []


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
) -> list[BaseExpertNode]:
    """Factory function to create all expert nodes."""
    return [
        PromptHardeningNode(model),
        OwaspApiNode(model),
        OwaspWebNode(model),
        OwaspMobileNode(model),
        DevSecOpsNode(model),
        CodeVulnerabilitiesNode(model),
    ]

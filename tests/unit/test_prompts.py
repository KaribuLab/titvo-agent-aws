"""Tests for PromptRegistry module."""

import pytest

from code_analysis import prompts
from code_analysis.prompts import PromptRegistry, get_registry


class TestPromptRegistry:
    """Tests for PromptRegistry functionality."""

    def test_singleton_pattern(self):
        """PromptRegistry should be a singleton."""
        r1 = PromptRegistry()
        r2 = PromptRegistry()
        assert r1 is r2

    def test_get_registry_convenience(self):
        """get_registry should return the global instance."""
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_system_prompt_loads(self):
        """System prompt should load successfully."""
        prompt = prompts.get_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 100
        assert "Titvo" in prompt
        assert "JSON" in prompt
        assert "Security Boundary" in prompt
        assert "Anti-Fabrication" in prompt

    def test_content_template_loads(self):
        """Content template should load successfully."""
        template = prompts.get_content_template()
        assert isinstance(template, str)
        assert "{repository_url}" in template
        assert "{commit_hash}" in template

    def test_orchestrator_prompt_is_gone(self):
        """The dead orchestrator prompt was removed."""
        assert not hasattr(prompts, "get_orchestrator_prompt")
        assert not hasattr(PromptRegistry(), "get_orchestrator_prompt")
        import importlib.resources as resources

        assert (
            not resources.files("code_analysis.prompts")
            .joinpath("orchestrator_prompt.md")
            .is_file()
        )

    def test_common_preamble_defines_runtime_and_suspicion_policy(self):
        preamble = prompts.get_common_preamble()
        assert preamble == prompts.get_system_prompt()
        assert "[runtime:" in preamble
        assert "browser" in preamble and "mobile" in preamble
        assert "import.meta.env" in preamble
        assert "Sospecha: " in preamble
        assert "Para confirmar: " in preamble
        assert "Autenticación basada en secretos del lado cliente" in preamble
        assert "RAG CONTEXT" in preamble
        # No legacy monolithic-flow fields.
        assert "scaned_files" not in preamble

    def test_compose_expert_prompt_is_preamble_plus_domain(self):
        for name in prompts.list_experts():
            composed = prompts.compose_expert_prompt(name)
            preamble = prompts.get_common_preamble().rstrip()
            expert = prompts.get_expert_prompt(name).lstrip()
            assert composed.startswith(preamble)
            assert composed.endswith(expert)
            assert "\n\n---\n\n" in composed

    def test_no_expert_keeps_absolute_env_var_exclusion(self):
        for name in prompts.list_experts():
            text = prompts.get_expert_prompt(name)
            assert (
                "Environment variable references for secrets → NOT a finding"
                not in text
            )
            assert (
                "Environment variable references or placeholder values → NOT a finding"
                not in text
            )

    def test_client_side_experts_mention_runtime_and_client_auth(self):
        for name in ("owasp_api", "owasp_web", "owasp_mobile"):
            text = prompts.get_expert_prompt(name)
            assert "[runtime:" in text, name
            assert "Autenticación basada en secretos del lado cliente" in text, name
        for name in ("code_vulnerabilities", "devsecops"):
            assert "runtime" in prompts.get_expert_prompt(name).lower(), name

    def test_consolidation_prompt_handles_suspicions(self):
        prompt = prompts.get_findings_consolidation_prompt()
        assert "Sospecha:" in prompt
        assert "todas" in prompt
        assert "Para confirmar:" in prompt

    def test_findings_consolidation_prompt_loads(self):
        """Findings consolidation prompt should load successfully."""
        prompt = prompts.get_findings_consolidation_prompt()
        assert isinstance(prompt, str)
        assert "{{ findings_json }}" in prompt
        assert "Consolidación" in prompt
        assert "causa raíz" in prompt
        assert "source_ids" in prompt
        assert "mantén los hallazgos separados" in prompt
        assert "agresiva" not in prompt

    def test_all_expert_prompts_load(self):
        """All expert prompts should load successfully."""
        expert_names = prompts.list_experts()
        assert len(expert_names) == 6

        for expert_name in expert_names:
            prompt = prompts.get_expert_prompt(expert_name)
            assert isinstance(prompt, str)
            assert len(prompt) > 100

    def test_expert_prompt_content(self):
        """Expert prompts should have expected content."""
        # Prompt hardening
        ph = prompts.get_expert_prompt("prompt_hardening")
        assert "prompt injection" in ph.lower() or "jailbreak" in ph.lower()

        # OWASP API
        oapi = prompts.get_expert_prompt("owasp_api")
        assert "OWASP API" in oapi or "API Security" in oapi

        # OWASP Web
        oweb = prompts.get_expert_prompt("owasp_web")
        assert "OWASP Web" in oweb or "Web Top 10" in oweb

        # OWASP Mobile
        omobile = prompts.get_expert_prompt("owasp_mobile")
        assert "OWASP Mobile" in omobile or "MASVS" in omobile

        # DevSecOps
        dev = prompts.get_expert_prompt("devsecops")
        assert "DevSecOps" in dev or "CI/CD" in dev or "Infrastructure" in dev

        # Code Vulnerabilities
        cv = prompts.get_expert_prompt("code_vulnerabilities")
        assert "vulnerability" in cv.lower() or "injection" in cv.lower()

    def test_invalid_expert_raises_error(self):
        """Invalid expert name should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            prompts.get_expert_prompt("invalid_expert")

        assert "invalid_expert" in str(exc_info.value)
        assert "Valid options" in str(exc_info.value)

    def test_list_experts(self):
        """list_experts should return all expert names."""
        experts = prompts.list_experts()
        assert isinstance(experts, list)
        assert "prompt_hardening" in experts
        assert "owasp_api" in experts
        assert "owasp_web" in experts
        assert "owasp_mobile" in experts
        assert "devsecops" in experts
        assert "code_vulnerabilities" in experts


class TestPromptCaching:
    """Tests for prompt caching behavior."""

    def test_prompts_are_cached(self):
        """Prompts should be cached (same object on multiple calls)."""
        r = PromptRegistry()
        p1 = r.get_system_prompt()
        p2 = r.get_system_prompt()
        # @cache decorator returns same object
        assert p1 == p2

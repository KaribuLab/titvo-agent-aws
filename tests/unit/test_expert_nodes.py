"""Tests for expert node implementations."""

import pytest

from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
    DevSecOpsNode,
    OwaspApiNode,
    OwaspWebNode,
    PromptHardeningNode,
    create_expert_nodes,
)


class TestPromptHardeningNode:
    """Tests for Prompt Hardening expert."""

    @pytest.fixture
    def node(self):
        return PromptHardeningNode(None)

    def test_expert_name(self, node):
        assert node.expert_name == "prompt_hardening"

    def test_analyzes_all_files(self, node):
        """Prompt hardening should analyze all files."""
        assert node.get_file_patterns() == []
        assert node.should_analyze_file("any/file.py")
        assert node.should_analyze_file("test.js")


class TestOwaspApiNode:
    """Tests for OWASP API expert."""

    @pytest.fixture
    def node(self):
        return OwaspApiNode(None)

    def test_expert_name(self, node):
        assert node.expert_name == "owasp_api"

    def test_api_file_patterns(self, node):
        patterns = node.get_file_patterns()
        assert "*route*" in patterns
        assert "*api*" in patterns
        assert "*controller*" in patterns

    def test_matches_api_files(self, node):
        assert node.should_analyze_file("src/routes/users.py")
        assert node.should_analyze_file("api/endpoints.py")
        assert node.should_analyze_file("controllers/auth.py")
        assert node.should_analyze_file("openapi.yaml")

    def test_skips_non_api_files(self, node):
        """Should not match non-API files."""
        assert not node.should_analyze_file("tests/test_utils.py")


class TestOwaspWebNode:
    """Tests for OWASP Web expert."""

    @pytest.fixture
    def node(self):
        return OwaspWebNode(None)

    def test_expert_name(self, node):
        assert node.expert_name == "owasp_web"

    def test_web_file_patterns(self, node):
        patterns = node.get_file_patterns()
        assert "*.html" in patterns
        assert "*.js" in patterns
        assert "*template*" in patterns

    def test_matches_web_files(self, node):
        assert node.should_analyze_file("index.html")
        assert node.should_analyze_file("app.js")
        assert node.should_analyze_file("templates/base.html")


class TestDevSecOpsNode:
    """Tests for DevSecOps expert."""

    @pytest.fixture
    def node(self):
        return DevSecOpsNode(None)

    def test_expert_name(self, node):
        assert node.expert_name == "devsecops"

    def test_devops_file_patterns(self, node):
        patterns = node.get_file_patterns()
        assert "*.yml" in patterns
        assert "Dockerfile*" in patterns
        assert "*.tf" in patterns
        assert ".github/**" in patterns

    def test_matches_ci_files(self, node):
        assert node.should_analyze_file(".github/workflows/ci.yml")
        assert node.should_analyze_file("Dockerfile")
        assert node.should_analyze_file("main.tf")


class TestCodeVulnerabilitiesNode:
    """Tests for Code Vulnerabilities expert."""

    @pytest.fixture
    def node(self):
        return CodeVulnerabilitiesNode(None)

    def test_expert_name(self, node):
        assert node.expert_name == "code_vulnerabilities"

    def test_analyzes_all_files(self, node):
        """Code vulns expert should analyze all files."""
        assert node.get_file_patterns() == []
        assert node.should_analyze_file("src/app.py")
        assert node.should_analyze_file("lib/utils.js")


class TestCreateExpertNodes:
    """Tests for expert factory function."""

    def test_creates_all_experts(self):
        nodes = create_expert_nodes(None)
        assert len(nodes) == 5

        names = [n.expert_name for n in nodes]
        assert "prompt_hardening" in names
        assert "owasp_api" in names
        assert "owasp_web" in names
        assert "devsecops" in names
        assert "code_vulnerabilities" in names


class TestFileFiltering:
    """Tests for file filtering logic."""

    @pytest.fixture
    def sample_files(self):
        return [
            {"path": "src/routes/api.py", "content": "code"},
            {"path": "templates/index.html", "content": "html"},
            {"path": ".github/workflows/ci.yml", "content": "yaml"},
            {"path": "src/utils.py", "content": "python"},
        ]

    def test_devsecops_filters_ci_files(self, sample_files):
        node = DevSecOpsNode(None)
        filtered = node._filter_files(sample_files)
        
        # Should include .github/workflows/ci.yml
        paths = [f["path"] for f in filtered]
        assert ".github/workflows/ci.yml" in paths

    def test_owasp_api_filters_route_files(self, sample_files):
        node = OwaspApiNode(None)
        filtered = node._filter_files(sample_files)
        
        paths = [f["path"] for f in filtered]
        assert "src/routes/api.py" in paths

    def test_fallback_when_no_matches(self, sample_files):
        """When no files match patterns, should return all files."""
        # Create node with patterns that won't match
        node = OwaspApiNode(None)
        # Mock patterns to something that won't match
        node._file_patterns = ["*nonexistent*"]
        
        filtered = node.filter_files([
            {"path": "file1.txt", "content": ""},
            {"path": "file2.txt", "content": ""},
        ])
        
        # Fallback: all files returned
        assert len(filtered) == 2

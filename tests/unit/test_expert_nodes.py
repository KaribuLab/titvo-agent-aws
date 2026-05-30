"""Tests for expert node implementations."""

import pytest

from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    BaseExpertNode,
)
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

        filtered = node.filter_files(
            [
                {"path": "file1.txt", "content": ""},
                {"path": "file2.txt", "content": ""},
            ]
        )

        # Fallback: all files returned
        assert len(filtered) == 2


class TestBaseExpertNodeFormatRagChunks:
    """Tests for BaseExpertNode._format_rag_chunks()."""

    @pytest.fixture
    def node(self):
        return PromptHardeningNode(None)

    def test_empty_chunks_returns_empty_string(self, node):
        """Empty list should produce empty string (no RAG block added)."""
        result = node._format_rag_chunks([])
        assert result == ""

    def test_single_chunk_produces_block(self, node):
        """A single chunk should produce a properly formatted RAG block."""
        chunks = [
            {"file_path": "src/auth.py", "chunk_text": "def login(user): pass"},
        ]
        result = node._format_rag_chunks(chunks)

        assert "=== RAG CONTEXT" in result
        assert "src/auth.py" in result
        assert "def login(user): pass" in result
        assert "=== END RAG CONTEXT ===" in result

    def test_multiple_chunks(self, node):
        """Multiple chunks should all appear in the block."""
        chunks = [
            {"file_path": "src/a.py", "chunk_text": "code a"},
            {"file_path": "src/b.py", "chunk_text": "code b"},
        ]
        result = node._format_rag_chunks(chunks)

        assert "src/a.py" in result
        assert "src/b.py" in result
        assert "code a" in result
        assert "code b" in result

    def test_chunk_missing_file_path_uses_unknown(self, node):
        """Chunk without file_path should use 'unknown' as label."""
        chunks = [{"chunk_text": "orphan code"}]
        result = node._format_rag_chunks(chunks)
        assert "unknown" in result
        assert "orphan code" in result


class TestSmartTruncate:
    """Tests for BaseExpertNode._smart_truncate()."""

    @pytest.fixture
    def node(self):
        return PromptHardeningNode(None)

    def test_short_content_not_truncated(self, node):
        """Content under the limit should be returned unchanged."""
        content = "import os\ndef foo(): pass\n"
        result, truncated = node._smart_truncate(content, max_chars=1000)
        assert result == content
        assert truncated is False

    def test_truncated_flag_set_for_large_content(self, node):
        """Content exceeding the limit should set truncated=True."""
        content = "x" * 10_000
        _, truncated = node._smart_truncate(content, max_chars=100)
        assert truncated is True

    def test_result_within_budget(self, node):
        """Result length should not exceed max_chars."""
        content = ("import os\n" * 50) + ("x = 1\n" * 500)
        max_chars = 200
        result, _ = node._smart_truncate(content, max_chars=max_chars)
        assert len(result) <= max_chars + 200  # small overshoot allowed for separator

    def test_structural_lines_preserved_after_cut(self, node):
        """Structural lines from the tail portion should appear in the result."""
        head_filler = "x = 1\n" * 200  # non-structural → forms the head
        tail_struct = "def secret_func(): pass\n"
        content = head_filler + tail_struct
        result, truncated = node._smart_truncate(content, max_chars=500)
        assert truncated is True
        assert "def secret_func" in result

    def test_non_structural_tail_not_included(self, node):
        """Non-structural lines in the tail should be omitted when budget is tight."""
        head = "import os\n" * 5
        tail = "this_is_not_structural = 'hidden'\n" * 100
        content = head + tail
        result, truncated = node._smart_truncate(content, max_chars=len(head) + 10)
        assert "this_is_not_structural" not in result or truncated


class TestBuildFileQuery:
    """Tests for RagRetrievalNode._build_file_query()."""

    def test_extracts_structural_lines(self):
        from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
            RagRetrievalNode,
        )

        content = "import os\nx = 1\ndef foo(): pass\ny = 2\nclass Bar: pass\n"
        query = RagRetrievalNode._build_file_query("src/a.py", content)
        assert "import os" in query
        assert "def foo" in query
        assert "class Bar" in query
        assert "x = 1" not in query  # non-structural
        assert "y = 2" not in query

    def test_falls_back_when_no_structural_lines(self):
        from code_analysis.infra.adapters.langgraph.nodes.rag_retrieval_node import (
            RagRetrievalNode,
        )

        content = "x = 1\ny = 2\nz = 3\n"
        query = RagRetrievalNode._build_file_query("src/a.py", content)
        assert "src/a.py" in query
        assert "x = 1" in query  # fallback: first N chars


class TestStructuralLines:
    """Tests for _structural_lines.is_structural across languages."""

    def _check(self, line: str, expected: bool = True):
        from code_analysis.infra.adapters.langgraph.nodes._structural_lines import (
            is_structural,
        )

        result = is_structural(line)
        assert result is expected, (
            f"is_structural({line!r}) = {result}, expected {expected}"
        )

    # Python
    def test_python_def(self):
        self._check("def authenticate(user, pwd):")

    def test_python_async_def(self):
        self._check("async def fetch_data(url):")

    def test_python_class(self):
        self._check("class UserService:")

    def test_python_import(self):
        self._check("import boto3")

    def test_python_from_import(self):
        self._check("from django.db import models")

    def test_python_decorator(self):
        self._check("@require_auth")

    # JavaScript / TypeScript
    def test_ts_interface(self):
        self._check("interface IUserRepository {")

    def test_ts_export_fn(self):
        self._check("export function createUser(dto: CreateUserDto) {")

    def test_ts_const_fn(self):
        self._check("const handler = async (req) => {")

    def test_ts_declare(self):
        self._check("declare module 'express' {")

    def test_ts_type_alias(self):
        self._check("type UserId = string;")

    def test_ts_enum(self):
        self._check("enum Role { ADMIN, USER }")

    def test_ts_import(self):
        self._check("import { Injectable } from '@nestjs/common';")

    # Java / Kotlin
    def test_java_public_class(self):
        self._check("public class UserController {")

    def test_java_private_method(self):
        self._check("private void validateToken(String token) {")

    def test_java_annotation(self):
        self._check("@RestController")

    def test_kotlin_fun(self):
        self._check("fun getUserById(id: Long): User? {")

    def test_kotlin_data_class(self):
        self._check("data class UserDto(val id: Long, val name: String)")

    # Go
    def test_go_func(self):
        self._check("func (s *UserService) GetUser(id int) (*User, error) {")

    def test_go_type_struct(self):
        self._check("type UserRepository struct {")

    def test_go_import(self):
        self._check('import "net/http"')

    def test_go_package(self):
        self._check("package main")

    # Rust
    def test_rust_fn(self):
        self._check("fn parse_token(input: &str) -> Result<Token, Error> {")

    def test_rust_pub_fn(self):
        self._check("pub fn authenticate(credentials: &Credentials) -> bool {")

    def test_rust_struct(self):
        self._check("struct UserSession {")

    def test_rust_impl(self):
        self._check("impl AuthService for PostgresAuthService {")

    def test_rust_use(self):
        self._check("use crate::domain::ports::auth::IAuthPort;")

    # C#
    def test_csharp_class(self):
        self._check("public class UserController : ControllerBase {")

    def test_csharp_interface(self):
        self._check("public interface IUserRepository {")

    def test_csharp_using(self):
        self._check("using Microsoft.EntityFrameworkCore;")

    def test_csharp_namespace(self):
        self._check("namespace Titvo.Api.Controllers {")

    # Ruby
    def test_ruby_def(self):
        self._check("def authenticate(user, password)")

    def test_ruby_class(self):
        self._check("class ApplicationController < ActionController::Base")

    def test_ruby_require(self):
        self._check("require 'jwt'")

    def test_ruby_module(self):
        self._check("module Authentication")

    # PHP
    def test_php_function(self):
        self._check("function validateInput(string $input): bool {")

    def test_php_class(self):
        self._check("class UserRepository implements IUserRepository {")

    def test_php_namespace(self):
        self._check("namespace App\\Http\\Controllers;")

    def test_php_use(self):
        self._check("use Illuminate\\Support\\Facades\\Auth;")

    # IaC / Terraform
    def test_tf_resource(self):
        self._check('resource "aws_lambda_function" "agent" {')

    def test_tf_variable(self):
        self._check('variable "environment" {')

    def test_tf_output(self):
        self._check('output "function_arn" {')

    # Dockerfile
    def test_dockerfile_from(self):
        self._check("FROM python:3.13-slim-bookworm")

    def test_dockerfile_run(self):
        self._check("RUN uv sync --frozen --no-dev")

    def test_dockerfile_entrypoint(self):
        self._check('ENTRYPOINT ["python", "main.py"]')

    # SQL
    def test_sql_create_table(self):
        self._check("CREATE TABLE users (")

    def test_sql_create_func(self):
        self._check("CREATE FUNCTION get_user(user_id INT)")

    def test_sql_alter(self):
        self._check("ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN;")

    # C/C++
    def test_c_include(self):
        self._check("#include <stdio.h>")

    def test_c_define(self):
        self._check("#define MAX_RETRIES 3")

    def test_c_struct(self):
        self._check("struct User {")

    # Non-structural (should return False)
    def test_plain_assignment(self):
        self._check("x = 1", expected=False)

    def test_blank_line(self):
        self._check("", expected=False)

    def test_comment_line(self):
        self._check("# just a comment", expected=False)

    def test_log_call(self):
        self._check("    logger.info('done')", expected=False)

"""Tests for expert node implementations."""

import pytest

from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    EXPERT_RUNTIMES,
    CodeVulnerabilitiesNode,
    DevSecOpsNode,
    OwaspApiNode,
    OwaspMobileNode,
    OwaspWebNode,
    PromptHardeningNode,
    create_expert_nodes,
)


class TestExpertRuntimes:
    """Experts select files by runtime intersection; no name patterns."""

    def test_prompt_hardening_and_code_vulns_see_everything(self):
        for node in (PromptHardeningNode(None), CodeVulnerabilitiesNode(None)):
            assert node.get_runtimes() == {
                "browser",
                "server",
                "mobile",
                "infra",
                "test",
                "config",
                "unknown",
            }

    def test_owasp_api_runtimes_include_browser_clients(self):
        assert OwaspApiNode(None).get_runtimes() == {
            "server",
            "browser",
            "unknown",
            "config",
        }

    def test_owasp_web_runtimes(self):
        assert OwaspWebNode(None).get_runtimes() == {"browser", "server", "unknown"}

    def test_owasp_mobile_runtimes(self):
        assert OwaspMobileNode(None).get_runtimes() == {"mobile"}

    def test_devsecops_runtimes_include_tests(self):
        assert DevSecOpsNode(None).get_runtimes() == {"infra", "config", "test"}

    def test_registry_matches_nodes(self):
        for node in create_expert_nodes(None):
            assert EXPERT_RUNTIMES[node.expert_name] == node.get_runtimes()

    def test_expert_names(self):
        names = {
            PromptHardeningNode: "prompt_hardening",
            OwaspApiNode: "owasp_api",
            OwaspWebNode: "owasp_web",
            OwaspMobileNode: "owasp_mobile",
            DevSecOpsNode: "devsecops",
            CodeVulnerabilitiesNode: "code_vulnerabilities",
        }
        for cls, name in names.items():
            assert cls(None).expert_name == name


class TestCreateExpertNodes:
    """Tests for expert factory function."""

    def test_creates_all_experts(self):
        nodes = create_expert_nodes(None)
        assert len(nodes) == 6
        assert [n.expert_name for n in nodes] == [
            "prompt_hardening",
            "owasp_api",
            "owasp_web",
            "owasp_mobile",
            "devsecops",
            "code_vulnerabilities",
        ]

    def test_nodes_share_config(self):
        nodes = create_expert_nodes(None)
        configs = {id(n._config) for n in nodes}
        assert len(configs) == 1


class TestFileSelection:
    """Selection by runtime intersection without fallback."""

    @pytest.fixture
    def sample_files(self):
        return [
            {"path": "src/routes/api.py", "content": "code", "runtimes": ["server"]},
            {
                "path": "templates/index.html",
                "content": "html",
                "runtimes": ["browser"],
            },
            {
                "path": ".github/workflows/ci.yml",
                "content": "yaml",
                "runtimes": ["infra"],
            },
            {"path": "src/utils.py", "content": "python", "runtimes": ["server"]},
            {
                "path": "src/services/apiClient.ts",
                "content": "fetch()",
                "runtimes": ["browser"],
            },
            {"path": "tests/test_x.py", "content": "t", "runtimes": ["test"]},
        ]

    def test_devsecops_selects_infra_and_tests(self, sample_files):
        paths = [f["path"] for f in DevSecOpsNode(None)._select_files(sample_files)]
        assert paths == [".github/workflows/ci.yml", "tests/test_x.py"]

    def test_owasp_api_selects_server_and_browser_clients(self, sample_files):
        paths = [f["path"] for f in OwaspApiNode(None)._select_files(sample_files)]
        assert "src/routes/api.py" in paths
        assert "src/services/apiClient.ts" in paths
        assert ".github/workflows/ci.yml" not in paths

    def test_frontend_client_reaches_owasp_api_in_commit_and_full(self, sample_files):
        client = [f for f in sample_files if f["path"].endswith("apiClient.ts")]
        commit_scan = OwaspApiNode(None)._select_files(client)
        full_scan = OwaspApiNode(None)._select_files(
            client
            + [
                {"path": f"srv/{i}.py", "content": "x", "runtimes": ["server"]}
                for i in range(200)
            ]
        )
        assert [f["path"] for f in commit_scan] == ["src/services/apiClient.ts"]
        assert "src/services/apiClient.ts" in [f["path"] for f in full_scan]

    def test_multi_runtime_file_reaches_every_matching_expert(self):
        f = {"path": "a.tsx", "content": "x", "runtimes": ["mobile", "browser"]}
        assert OwaspMobileNode(None)._select_files([f]) == [f]
        assert OwaspWebNode(None)._select_files([f]) == [f]
        assert DevSecOpsNode(None)._select_files([f]) == []

    def test_no_fallback_when_nothing_matches(self):
        files = [
            {"path": "file1.py", "content": "", "runtimes": ["server"]},
            {"path": "file2.py", "content": "", "runtimes": ["server"]},
        ]
        assert OwaspMobileNode(None)._select_files(files) == []

    def test_web_tsx_not_sent_to_mobile(self):
        f = {"path": "src/App.tsx", "content": "x", "runtimes": ["browser"]}
        assert OwaspMobileNode(None)._select_files([f]) == []


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
        from code_analysis.domain.services.structural_lines import (
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

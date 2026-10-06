"""Tests for the classify_runtime node."""

import json

from code_analysis.infra.adapters.langgraph.nodes.classify_runtime_node import (
    ClassifyRuntimeNode,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import EXPERT_RUNTIMES


def _node() -> ClassifyRuntimeNode:
    return ClassifyRuntimeNode(EXPERT_RUNTIMES)


def test_all_files_get_runtimes_and_content_is_untouched():
    files = [
        {"path": "src/App.tsx", "content": "import React from 'react'"},
        {"path": "src/api.py", "content": "from flask import Flask"},
        {"path": "Dockerfile", "content": "FROM python"},
    ]
    result = _node()({"files": files, "issues": []})

    out = result["files"]
    assert [f["runtimes"] for f in out] == [["browser"], ["server"], ["infra"]]
    assert [(f["path"], f["content"]) for f in out] == [
        (f["path"], f["content"]) for f in files
    ]


def test_coverage_invariant_holds_and_is_reported():
    files = [
        {"path": "src/App.tsx", "content": "import React from 'react'"},
        {"path": "src/util.ts", "content": "export const a = 1"},
        {"path": "lib/main.dart", "content": "void main() {}"},
        {"path": ".github/workflows/ci.yml", "content": "on: push"},
        {"path": "tests/test_a.py", "content": "def test(): pass"},
    ]
    result = _node()({"files": files, "issues": []})
    coverage = result["expert_metadata"]["coverage"]

    assert coverage["files_without_domain_expert"] == 0
    assert coverage["prompt_hardening"] == 5
    assert coverage["code_vulnerabilities"] == 5
    assert coverage["owasp_api"] == 2  # browser + unknown
    assert coverage["owasp_web"] == 2
    assert coverage["owasp_mobile"] == 1
    assert coverage["devsecops"] == 2  # infra + test


def test_project_profile_recorded_and_applied():
    files = [
        {
            "path": "package.json",
            "content": json.dumps({"dependencies": {"react-native": "0.74"}}),
        },
        {"path": "src/components/Card.tsx", "content": "import React from 'react'"},
    ]
    result = _node()({"files": files, "issues": []})
    assert result["expert_metadata"]["project_profile"]["mobile_js"] is True
    card = next(f for f in result["files"] if f["path"].endswith("Card.tsx"))
    assert card["runtimes"] == ["mobile", "browser"]


def test_empty_files():
    result = _node()({"files": [], "issues": []})
    assert result["files"] == []
    assert result["expert_metadata"]["coverage"]["files_without_domain_expert"] == 0

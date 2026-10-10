"""Regressions for invalid model findings, bounded recovery and preserved coverage."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_response import parse_response
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)

FILES = [{"path": "src/app.py", "content": "name = input()\neval(name)\n"}]
ISSUE = {
    "title": "Untrusted eval",
    "severity": "HIGH",
    "category": "RCE",
    "path": "src/app.py",
    "line": 2,
    "code": "eval(name)",
}


def response(data):
    """Build a provider-shaped response without making a remote request."""
    return SimpleNamespace(content=json.dumps(data))


def model(*answers):
    """Count exact provider calls while exercising the actual expert node."""
    return SimpleNamespace(ainvoke=AsyncMock(side_effect=answers))


def test_normalizes_exact_paths_and_numeric_lines_without_provider_retry():
    result = parse_response(
        json.dumps(
            {
                "issues": [{**ISSUE, "path": " ./src\\app.py ", "line": " 2 "}],
            }
        ),
        FILES,
        "test",
    )
    assert result.error is None
    assert result.issues[0].path == "src/app.py"
    assert result.issues[0].line == 2


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"path": "../src/app.py"}, "traversal"),
        ({"path": "missing.py"}, "not in this batch"),
        ({"line": True}, "positive integer"),
        ({"line": "2-3"}, "positive integer"),
        ({"line": 500}, "original file length"),
        ({"severity": None}, "nonempty string"),
        ({"severity": "unknown"}, "CRITICAL"),
    ],
)
def test_precise_rejection_reasons(changes, reason):
    result = parse_response(
        json.dumps({"issues": [{**ISSUE, **changes}]}), FILES, "test"
    )
    assert reason in result.error
    assert result.rejected[0]["source_id"] == 0
    assert not result.issues


@pytest.mark.asyncio
async def test_repair_missing_field_preserves_valid_original_finding():
    valid = {**ISSUE, "title": "Existing finding"}
    invalid = {key: value for key, value in ISSUE.items() if key != "category"}
    provider = model(
        response({"issues": [valid, invalid]}),
        response(
            {
                "repairs": [{"source_id": 1, "issue": ISSUE}],
            }
        ),
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert not output["expert_errors"]
    assert [issue.title for issue in output["issues"]] == [
        "Existing finding",
        ISSUE["title"],
    ]
    metadata = output["expert_metadata"]["code_vulnerabilities"]
    assert metadata["batches_completed"] == 1
    assert metadata["repair_attempts"] == metadata["batches_repaired"] == 1
    assert (
        metadata["batch_diagnostics"][0]["rejections"][0]["reason"]
        == "missing fields: category"
    )
    assert "finding" not in metadata["batch_diagnostics"][0]["rejections"][0]
    assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repairs",
    [
        [],
        [{"source_id": 99, "issue": ISSUE}],
        [{"source_id": 0, "issue": ISSUE}, {"source_id": 0, "issue": ISSUE}],
        [{"source_id": 0, "issue": {**ISSUE, "path": "other.py"}}],
        [{"source_id": 0, "issue": {**ISSUE, "code": "made up code"}}],
        [{"source_id": 0, "error": "unsupported"}],
    ],
)
async def test_failed_repair_cannot_hide_failure_or_discard_valid_findings(repairs):
    valid = {**ISSUE, "title": "Keep me"}
    invalid = {key: value for key, value in ISSUE.items() if key != "category"}
    provider = model(
        response({"issues": [invalid, valid]}), response({"repairs": repairs})
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert [issue.title for issue in output["issues"]] == ["Keep me"]
    assert output["expert_errors"]
    assert output["expert_metadata"]["code_vulnerabilities"]["batches_completed"] == 0
    final = MergeFindingsNode()(dict(output, scaned_files=1))["final_output"]
    assert not final["coverage"]["complete"]
    assert provider.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_repair_keeps_successful_corrections_when_one_is_omitted():
    first = {key: value for key, value in ISSUE.items() if key != "category"}
    second = {**first, "title": "Second finding"}
    provider = model(
        response({"issues": [first, second]}),
        response(
            {
                "repairs": [{"source_id": 0, "issue": ISSUE}],
            }
        ),
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert len(output["issues"]) == 1
    assert "omitted" in output["expert_errors"][0]
    assert "batch 1" in output["expert_errors"][0]


@pytest.mark.asyncio
async def test_unknown_path_can_be_corrected_only_with_existing_literal_evidence():
    provider = model(
        response({"issues": [{**ISSUE, "path": "app.py"}]}),
        response(
            {
                "repairs": [{"source_id": 0, "issue": ISSUE}],
            }
        ),
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert not output["expert_errors"]
    assert output["issues"][0].path == "src/app.py"


@pytest.mark.asyncio
async def test_repair_preserves_severity_and_has_bounded_input():
    invalid = {key: value for key, value in ISSUE.items() if key != "category"}
    provider = model(
        response({"issues": [invalid]}),
        response(
            {
                "repairs": [{"source_id": 0, "issue": {**ISSUE, "severity": "LOW"}}],
            }
        ),
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert output["issues"][0].severity == "HIGH"
    messages = provider.ainvoke.await_args.args[0]
    assert sum(len(message.content) for message in messages) <= 200000


@pytest.mark.asyncio
async def test_oversized_invalid_response_does_not_trigger_unbounded_repair():
    provider = model(
        response(
            {"issues": [{**ISSUE, "path": "missing.py", "description": "x" * 17000}]}
        )
    )
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert provider.ainvoke.await_count == 1
    assert "16000" in output["expert_errors"][0]


@pytest.mark.asyncio
async def test_valid_response_never_makes_a_repair_call():
    provider = model(response({"issues": [ISSUE]}))
    output = await CodeVulnerabilitiesNode(provider)({"files": FILES, "issues": []})
    assert not output["expert_errors"]
    assert provider.ainvoke.await_count == 1

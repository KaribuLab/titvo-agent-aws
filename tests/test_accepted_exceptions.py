from datetime import date

import pytest

from code_analysis.domain.entities.expert_result import ExpertIssue
from code_analysis.domain.services.accepted_exceptions import parse_policy
from code_analysis.infra.adapters.langgraph.nodes.merge_findings_node import (
    MergeFindingsNode,
)

POLICY = """version: 1
exceptions:
  - path: src/a.py
    line: 4
    category: crypto
    reason: Legacy integration
    accepted_by: Security team
    expires_on: "2099-01-01"
"""


def issue(path="src/a.py", line=4, category="crypto", severity="HIGH"):
    return ExpertIssue(
        "MD5",
        "Evidence",
        severity,
        category,
        path,
        line,
        "Summary",
        "md5(x)",
        "Replace",
    )


def scan(policy=None, issues=None, **extra):
    return MergeFindingsNode()(
        {
            "issues": [issue()] if issues is None else issues,
            "scaned_files": 1,
            "accepted_exceptions_text": policy,
            **extra,
        }
    )["final_output"]


def test_accepted_only_retains_evidence():
    result = scan(POLICY)
    assert result["status"] == "COMPLETED"
    assert result["issues"][0]["severity"] == "HIGH"
    assert result["issues"][0]["code"] == "md5(x)"
    assert result["issues"][0]["acceptance"] == {
        "reason": "Legacy integration",
        "accepted_by": "Security team",
        "expires_on": "2099-01-01",
    }
    assert result["accepted_issues_count"] == 1
    assert result["active_issues_count"] == 0


@pytest.mark.parametrize(
    "finding", [issue(path="other.py"), issue(line=5), issue(category="Crypto")]
)
def test_exact_selectors(finding):
    result = scan(POLICY, [finding])
    assert result["status"] == "FAILED"
    assert "acceptance" not in result["issues"][0]


@pytest.mark.parametrize(
    "severity,status",
    [
        ("LOW", "WARNING"),
        ("MEDIUM", "WARNING"),
        ("HIGH", "FAILED"),
        ("CRITICAL", "FAILED"),
    ],
)
def test_previous_severity_behavior(severity, status):
    assert scan(None, [issue(severity=severity)])["status"] == status
    assert scan(None, issues=[])["status"] == "COMPLETED"


def test_mixed_and_no_config():
    assert scan()["status"] == "FAILED"
    assert scan(POLICY, [issue(), issue(path="other.py")])["status"] == "FAILED"
    forged = issue()
    forged.metadata["acceptance"] = {"reason": "model"}
    assert "acceptance" not in scan(None, [forged])["issues"][0]


@pytest.mark.parametrize("source", ["cli", "github", "bitbucket"])
def test_notification_contract(source):
    from unittest.mock import MagicMock

    from code_analysis.domain.dto.result_dto import ResultDto
    from code_analysis.domain.notification_service import NotificationService

    repositories = [MagicMock() for _ in range(3)]
    repositories[0].create_code_insights_report.return_value = {
        "codeInsightsURL": "url"
    }
    repositories[1].create_github_issue.return_value = {"htmlURL": "url"}
    repositories[2].create_report.return_value = {"reportURL": "url"}
    result = scan(POLICY)
    service = NotificationService(*repositories)
    dto = ResultDto(source, {}, "sha", result["status"], 1, result["issues"])
    assert service.send_notifications(dto)["report_url"] == "url"
    normalized = repositories[2].create_report.call_args.args[0].issues[0]
    assert normalized.category == "crypto"
    assert normalized.acceptance == result["issues"][0]["acceptance"]
    assert normalized.severity == "HIGH"
    if source == "github":
        assert repositories[1].create_github_issue.call_args.args[0].issues == [
            normalized
        ]
    if source == "bitbucket":
        assert repositories[0].create_code_insights_report.call_args.args[
            0
        ].annotations == [normalized]


@pytest.mark.asyncio
async def test_cli_separates_policy_after_repository_validation():
    from unittest.mock import MagicMock

    from code_analysis.infra.adapters.langgraph.nodes.cli_retrieval_node import (
        CliRetrievalNode,
    )

    repository = MagicMock()
    repository.get_files.return_value = [
        {"path": ".titvoignore.yml", "content": POLICY},
        {"path": "src/a.py", "content": "code"},
    ]
    result = await CliRetrievalNode(repository)({"extra_args": {"batch_id": "batch"}})
    assert result["files"] == [{"path": "src/a.py", "content": "code"}]
    assert result["scaned_files"] == 1
    assert result["accepted_exceptions_text"] == POLICY


@pytest.mark.asyncio
async def test_git_policy_fixture_is_not_source():
    from unittest.mock import AsyncMock, MagicMock

    from code_analysis.infra.adapters.langgraph.nodes.mcp_retrieval_node import (
        MCPRetrievalNode,
    )

    tools = []
    for name, value in [
        ("git.commit-files", {"jobId": "job"}),
        (
            "git.commit-files.poll",
            {
                "status": "SUCCESS",
                "filesPaths": ["sha/.titvoignore.yml", "sha/src/a.py"],
            },
        ),
        ("files", None),
    ]:
        tool = MagicMock()
        tool.name = name
        tool.ainvoke = AsyncMock(return_value=value)
        tools.append(tool)
    tools[2].ainvoke.side_effect = [{"content": POLICY}, {"content": "code"}]
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=tools)
    result = await MCPRetrievalNode(client)(
        {"repository_url": "https://example.invalid/repo", "commit_hash": "sha"}
    )
    assert result["files"] == [{"path": "sha/src/a.py", "content": "code"}]
    assert result["accepted_exceptions_text"] == POLICY
    assert result["scaned_files"] == 1


@pytest.mark.parametrize(
    "text",
    [
        POLICY.replace("line: 4", "line: 0"),
        POLICY.replace("src/a.py", "src/*.py"),
        POLICY.replace("2099-01-01", "2099-02-30"),
        POLICY.replace("reason: Legacy integration", "reason: ''"),
        POLICY + POLICY.split("exceptions:\n")[1],
        "version: 1\nexceptions: &a [*a]",
    ],
)
def test_additional_policy_boundaries(text):
    with pytest.raises(ValueError, match="Invalid .titvoignore.yml"):
        parse_policy(text)


def test_expired_finding_is_active():
    result = scan(POLICY.replace("2099-01-01", "2000-01-01"))
    assert result["status"] == "FAILED"
    assert "acceptance" not in result["issues"][0]
    assert result["active_issues_count"] == 1


def test_commit_storage_prefix_preserves_evidence():
    result = scan(POLICY, [issue(path="sha/src/a.py")], commit_hash="sha")
    assert result["status"] == "COMPLETED"
    assert result["issues"][0]["path"] == "sha/src/a.py"


def test_expiry_inclusive():
    assert len(parse_policy(POLICY, today=date(2099, 1, 1))) == 1
    assert parse_policy(POLICY, today=date(2099, 1, 2)) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "version: 2\nexceptions: []",
        POLICY.replace('"2099-01-01"', "2099-01-01"),
        POLICY.replace("line: 4", "line: true"),
        POLICY.replace("src/a.py", "../a.py"),
        POLICY + "unknown: yes\n",
        "!!python/object/apply:os.system ['false']",
        "version: 1\nversion: 1\nexceptions: []",
    ],
)
def test_invalid_is_explicit_error(text):
    result = scan(text)
    assert result["status"] == "FAILED"
    assert "Invalid .titvoignore.yml" in result["error"]
    assert not result["coverage"]["complete"]


def test_infrastructure_and_incomplete_preserved():
    assert scan(POLICY, mcp_error="storage down")["error"] == "storage down"
    assert scan(POLICY, provider_error="outage")["status"] == "FAILED"
    assert (
        scan(
            POLICY,
            failed_batches=[
                {
                    "expert": "crypto",
                    "batch_index": 0,
                    "paths": ["a"],
                    "error": "timeout",
                }
            ],
        )["status"]
        == "WARNING"
    )

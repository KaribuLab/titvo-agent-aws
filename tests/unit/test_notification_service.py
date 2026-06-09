"""Tests for notification best-effort behavior."""

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from code_analysis.domain.dto.bitbucket_dto import BitbucketCodeInsightsInputDto
from code_analysis.domain.dto.result_dto import AnalysisStatus, ResultDto
from code_analysis.domain.entities.task_entity import TaskSource
from code_analysis.domain.notification_service import NotificationService
from code_analysis.infra.adapters.lambda_bitbucket_repository import (
    LambdaBitbucketRepository,
)


def _result_dto(scan_mode: str = "full") -> ResultDto:
    return ResultDto(
        source=TaskSource.BITBUCKET.value,
        args={
            "bitbucket_workspace": "workspace",
            "bitbucket_repo_slug": "repo",
            "bitbucket_commit": "abc123",
            "scan_mode": scan_mode,
        },
        commit_hash="abc123",
        status=AnalysisStatus.WARNING.value,
        scaned_files=1,
        issues=[
            {
                "path": "abc123/src/app.ts",
                "line": 10,
                "title": "Finding",
                "description": "Description",
                "severity": "MEDIUM",
                "type": "security",
                "code": "token",
                "summary": "Summary",
                "recommendation": "Fix",
            }
        ],
    )


def _service(bitbucket_repository: MagicMock) -> NotificationService:
    report_repository = MagicMock()
    report_repository.create_report.return_value = {
        "reportURL": "https://reports.example/report.html"
    }
    return NotificationService(
        bitbucket_repository=bitbucket_repository,
        github_repository=MagicMock(),
        report_repository=report_repository,
    )


def test_bitbucket_code_insights_failure_keeps_report_url():
    bitbucket_repository = MagicMock()
    bitbucket_repository.create_code_insights_report.side_effect = Exception(
        "Error creating report 400 Bad Request"
    )
    service = _service(bitbucket_repository)

    result = service.send_notifications(_result_dto())

    assert result["report_url"] == "https://reports.example/report.html"
    assert "400 Bad Request" in result["code_insights_error"]
    assert "code_insights_url" not in result


def test_bitbucket_code_insights_receives_scan_mode():
    bitbucket_repository = MagicMock()
    bitbucket_repository.create_code_insights_report.return_value = {
        "codeInsightsURL": "https://bitbucket.example/insights"
    }
    service = _service(bitbucket_repository)

    result = service.send_notifications(_result_dto(scan_mode="full"))

    sent_input = bitbucket_repository.create_code_insights_report.call_args.args[0]
    assert sent_input.scanMode == "full"
    assert result["code_insights_url"] == "https://bitbucket.example/insights"


def test_bitbucket_code_insights_missing_url_is_best_effort():
    bitbucket_repository = MagicMock()
    bitbucket_repository.create_code_insights_report.return_value = {}
    service = _service(bitbucket_repository)

    result = service.send_notifications(_result_dto())

    assert result["report_url"] == "https://reports.example/report.html"
    assert "missing codeInsightsURL" in result["code_insights_error"]


def test_lambda_bitbucket_repository_sends_scan_mode(monkeypatch):
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": BytesIO(
            b'{"codeInsightsURL":"https://bitbucket.example/insights"}'
        ),
    }
    monkeypatch.setattr(
        "code_analysis.infra.adapters.lambda_bitbucket_repository.boto3.client",
        lambda _service: lambda_client,
    )
    repository = LambdaBitbucketRepository("code-insights")

    repository.create_code_insights_report(
        BitbucketCodeInsightsInputDto(
            reportURL="https://reports.example/report.html",
            workspaceId="workspace",
            commitHash="abc123",
            repoSlug="repo",
            status=AnalysisStatus.WARNING.value,
            annotations=[],
            scanMode="full",
        )
    )

    payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert payload["scanMode"] == "full"


def test_lambda_bitbucket_repository_raises_error_payload(monkeypatch):
    lambda_client = MagicMock()
    lambda_client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": BytesIO(
            b'{"errorType":"Error","errorMessage":"Error creating report 400"}'
        ),
    }
    monkeypatch.setattr(
        "code_analysis.infra.adapters.lambda_bitbucket_repository.boto3.client",
        lambda _service: lambda_client,
    )
    repository = LambdaBitbucketRepository("code-insights")

    with pytest.raises(Exception, match="Error creating report 400"):
        repository.create_code_insights_report(
            BitbucketCodeInsightsInputDto(
                reportURL="https://reports.example/report.html",
                workspaceId="workspace",
                commitHash="abc123",
                repoSlug="repo",
                status=AnalysisStatus.WARNING.value,
                annotations=[],
            )
        )

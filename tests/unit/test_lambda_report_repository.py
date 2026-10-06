"""Tests for the report lambda payload."""

import json
from unittest.mock import MagicMock, patch

from code_analysis.domain.dto.result_dto import IssueDto, ResultDto


def _dto(**extra) -> ResultDto:
    return ResultDto(
        source="github",
        args={},
        commit_hash="abc",
        status="WARNING",
        scaned_files=2,
        issues=[
            IssueDto(
                path="a.ts",
                line=1,
                title="t",
                description="d",
                severity="MEDIUM",
                type="x",
                code="c",
                summary="s",
                recommendation="r",
            )
        ],
        **extra,
    )


def _invoke_and_capture(dto: ResultDto) -> dict:
    with patch("code_analysis.infra.adapters.lambda_report_repository.boto3") as boto3:
        from code_analysis.infra.adapters.lambda_report_repository import (
            LambdaReportRepository,
        )

        client = MagicMock()
        payload = MagicMock()
        payload.read.return_value = json.dumps({"reportURL": "http://r"}).encode()
        client.invoke.return_value = {"StatusCode": 200, "Payload": payload}
        boto3.client.return_value = client

        repo = LambdaReportRepository("fn")
        assert repo.create_report(dto) == {"reportURL": "http://r"}
        raw = client.invoke.call_args.kwargs["Payload"]
        return json.loads(raw)


def test_payload_without_incomplete_is_unchanged():
    payload = _invoke_and_capture(_dto())
    assert set(payload) == {"status", "annotations"}
    assert payload["annotations"][0]["path"] == "a.ts"


def test_payload_includes_incomplete_when_present():
    incomplete = {
        "failed_batches": [],
        "files_not_fully_analyzed": ["a.ts", "b.ts"],
        "message": "Análisis incompleto",
    }
    payload = _invoke_and_capture(_dto(incomplete=incomplete))
    assert payload["incomplete"] == incomplete

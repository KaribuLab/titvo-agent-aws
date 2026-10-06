"""Tests for AnalyseCodeUseCase scan mode and RAG freshness behavior."""

from unittest.mock import MagicMock

import pytest

from code_analysis.application.analyse_code_use_case import AnalyseCodeUseCase


class _Status:
    status = "SUCCEEDED"
    is_succeeded = True
    is_failed = False


def _make_use_case(rag_status, rag_trigger):
    return AnalyseCodeUseCase(
        task_repository=MagicMock(),
        agent=MagicMock(),
        notification_service=MagicMock(),
        content_template="",
        rag_index_status=rag_status,
        rag_indexer_trigger=rag_trigger,
    )


@pytest.mark.asyncio
async def test_commit_mode_uses_branch_index_only():
    rag_status = MagicMock()
    rag_status.is_indexed.return_value = True
    rag_trigger = MagicMock()
    use_case = _make_use_case(rag_status, rag_trigger)

    await use_case._ensure_rag_index(
        "https://github.com/org/repo", "main", "abc123", "commit"
    )

    rag_status.is_indexed.assert_called_once_with("https://github.com/org/repo", "main")
    rag_status.is_commit_indexed.assert_not_called()
    rag_trigger.trigger_full.assert_not_called()
    rag_trigger.trigger_delta.assert_not_called()


@pytest.mark.asyncio
async def test_full_mode_skips_indexing_when_commit_is_fresh():
    rag_status = MagicMock()
    rag_status.is_indexed.return_value = True
    rag_status.is_commit_indexed.return_value = True
    rag_trigger = MagicMock()
    use_case = _make_use_case(rag_status, rag_trigger)

    await use_case._ensure_rag_index(
        "https://github.com/org/repo", "main", "abc123", "full"
    )

    rag_status.is_commit_indexed.assert_called_once_with(
        "https://github.com/org/repo", "main", "abc123"
    )
    rag_trigger.trigger_delta.assert_not_called()


@pytest.mark.asyncio
async def test_full_mode_waits_for_delta_when_commit_is_stale(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(
        "code_analysis.application.analyse_code_use_case.asyncio.sleep", _no_sleep
    )

    rag_status = MagicMock()
    rag_status.is_indexed.return_value = True
    rag_status.is_commit_indexed.return_value = False
    rag_trigger = MagicMock()
    rag_trigger.trigger_delta.return_value = "delta-job-1"
    rag_trigger.get_job_status.return_value = _Status()
    use_case = _make_use_case(rag_status, rag_trigger)

    await use_case._ensure_rag_index(
        "https://github.com/org/repo", "main", "abc123", "full"
    )

    rag_trigger.trigger_delta.assert_called_once_with(
        "https://github.com/org/repo", "main", "abc123"
    )
    rag_trigger.get_job_status.assert_called_once_with("delta-job-1")


# --- ResultDto tolerance (scan-completeness) ---------------------------------


def test_result_dto_accepts_error_and_incomplete_from_final_output():
    from code_analysis.domain.dto.result_dto import ResultDto

    final_output = {
        "status": "FAILED",
        "scaned_files": 0,
        "issues": [],
        "error": "No files in commit",
    }
    dto = ResultDto(
        **{**final_output, "source": "github", "args": {}, "commit_hash": "abc"}
    )
    assert dto.error == "No files in commit"
    assert dto.incomplete is None

    incomplete = {"failed_batches": [], "files_not_fully_analyzed": ["a.ts"]}
    dto = ResultDto(
        status="WARNING",
        scaned_files=3,
        issues=[],
        source="github",
        args={},
        commit_hash="abc",
        incomplete=incomplete,
    )
    assert dto.incomplete == incomplete


@pytest.mark.asyncio
async def test_execute_persists_incomplete_in_task_result(monkeypatch):
    import json as _json
    from unittest.mock import AsyncMock

    from code_analysis.domain.entities.task_entity import Task, TaskSource

    incomplete = {
        "failed_batches": [
            {"expert": "owasp_api", "batch_index": 1, "paths": ["a.ts"]}
        ],
        "files_not_fully_analyzed": ["a.ts"],
        "message": "Análisis incompleto",
    }
    agent_output = {
        "status": "WARNING",
        "scaned_files": 3,
        "issues": [],
        "incomplete": incomplete,
    }

    task = MagicMock(spec=Task)
    task.branch = "main"
    task.repository_url = "https://github.com/org/repo"
    task.commit_hash = "abc123"
    task.args = {"scan_mode": "commit"}
    task.source = TaskSource.GITHUB

    task_repository = MagicMock()
    task_repository.get_task.return_value = task
    task_repository.update_task.side_effect = lambda t: t

    agent = MagicMock()
    agent.invoke = AsyncMock(
        return_value=MagicMock(content=_json.dumps(agent_output), metadata={})
    )
    notification_service = MagicMock()
    notification_service.send_notifications.return_value = {"report_url": "u"}
    rag_status = MagicMock()
    rag_status.is_indexed.return_value = True
    rag_status.is_commit_indexed.return_value = True

    use_case = AnalyseCodeUseCase(
        task_repository=task_repository,
        agent=agent,
        notification_service=notification_service,
        content_template="{repository_url}{commit_hash}{branch}{rag_context}{args}{files_content}",
        rag_index_status=rag_status,
        rag_indexer_trigger=MagicMock(),
    )

    await use_case.execute("task-1")

    sent_dto = notification_service.send_notifications.call_args.args[0]
    assert sent_dto.incomplete == incomplete
    stored = task.mark_completed.call_args.args[0]
    assert stored["incomplete"] == incomplete
    assert stored["report_url"] == "u"


@pytest.mark.asyncio
async def test_execute_mcp_error_marks_task_failed_instead_of_crashing():
    import json as _json
    from unittest.mock import AsyncMock

    from code_analysis.domain.entities.task_entity import Task, TaskSource

    task = MagicMock(spec=Task)
    task.branch = "main"
    task.repository_url = "https://github.com/org/repo"
    task.commit_hash = "abc123"
    task.args = {}
    task.source = TaskSource.GITHUB
    task_repository = MagicMock()
    task_repository.get_task.return_value = task
    task_repository.update_task.side_effect = lambda t: t

    agent = MagicMock()
    agent.invoke = AsyncMock(
        return_value=MagicMock(
            content=_json.dumps(
                {
                    "status": "FAILED",
                    "scaned_files": 0,
                    "issues": [],
                    "error": "No files in commit",
                }
            ),
            metadata={},
        )
    )
    notification_service = MagicMock()
    notification_service.send_notifications.return_value = {}
    rag_status = MagicMock()
    rag_status.is_indexed.return_value = True

    use_case = AnalyseCodeUseCase(
        task_repository=task_repository,
        agent=agent,
        notification_service=notification_service,
        content_template="",
        rag_index_status=rag_status,
        rag_indexer_trigger=MagicMock(),
    )

    await use_case.execute("task-1")

    task.mark_failed.assert_called_once()
    assert task.mark_failed.call_args.args[0]["error"] == "No files in commit"

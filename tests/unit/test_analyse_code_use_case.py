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

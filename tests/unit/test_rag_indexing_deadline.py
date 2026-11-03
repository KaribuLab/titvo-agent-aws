"""Exercise indexing deadlines with a controlled clock, never real sleeps."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import code_analysis.application.analyse_code_use_case as module


def status(name):
    """Represent the public status returned by the Batch trigger."""
    return SimpleNamespace(
        status=name, is_succeeded=name == "SUCCEEDED", is_failed=name == "FAILED"
    )


@pytest.fixture
def clock(monkeypatch):
    """Advance only during sleep or an explicitly simulated slow SDK call."""
    state = SimpleNamespace(now=1000.0, sleeps=[])

    async def sleep(seconds):
        """Record and advance the fake monotonic clock."""
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(module, "monotonic", lambda: state.now)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    return state


def use_case(timeout):
    """Create an analysis coordinator with explicit budget and fake IO."""
    result = module.AnalyseCodeUseCase(
        task_repository=MagicMock(),
        agent=MagicMock(),
        notification_service=MagicMock(),
        content_template="",
        rag_index_status=MagicMock(),
        rag_indexer_trigger=MagicMock(),
        rag_indexing_timeout_seconds=timeout,
    )
    result.rag_index_status.is_indexed.return_value = False
    result.rag_index_status.is_commit_indexed.return_value = False
    result.rag_indexer_trigger.get_job_status.return_value = status("RUNNING")
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget", [None, 0, -1, float("nan"), float("inf"), True, "600"]
)
@pytest.mark.parametrize("indexed", [True, False])
async def test_invalid_budget_never_submits_jobs(budget, indexed):
    """Reject even when an existing index would otherwise bypass the wait."""
    case = use_case(budget)
    case.rag_index_status.is_indexed.return_value = indexed
    with pytest.raises(ValueError, match="rag_indexing_timeout_seconds"):
        await case._ensure_rag_index("repo", "main", "abcdef123", "full")
    case.rag_indexer_trigger.trigger_full.assert_not_called()
    case.rag_indexer_trigger.trigger_delta.assert_not_called()


@pytest.mark.asyncio
async def test_success_after_old_limit(clock, caplog):
    """A 30-minute budget accepts success at 900 seconds, after 90 polls."""
    case = use_case(1800)
    case.rag_indexer_trigger.get_job_status.side_effect = [status("RUNNING")] * 89 + [
        status("SUCCEEDED")
    ]
    with caplog.at_level("INFO", logger=module.__name__):
        await case._wait_for_rag_job("job-long", "repo", "main")
    assert case.rag_indexer_trigger.get_job_status.call_count == 90
    assert clock.now == 1900
    assert "job-long status: RUNNING" in caplog.text
    assert "elapsed=890s, timeout=1800s" in caplog.text
    assert "Waiting for indexing job job-long (timeout=1800s)" in caplog.text
    assert "attempt" not in caplog.text


@pytest.mark.asyncio
async def test_failure_stops_immediately(clock):
    """FAILED must not be mistaken for a timeout or wait out the budget."""
    case = use_case(1800)
    case.rag_indexer_trigger.get_job_status.return_value = status("FAILED")
    with pytest.raises(RuntimeError, match="job-failed failed"):
        await case._wait_for_rag_job("job-failed", "repo", "main")
    assert clock.sleeps == [10]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget, sleeps, polls", [(2.5, [2.5], 0), (25, [10, 10, 5], 2), (20, [10, 10], 1)]
)
async def test_timeout_caps_final_sleep_and_identifies_job(
    clock, budget, sleeps, polls
):
    """No query starts at the deadline, even for fractional or exact intervals."""
    case = use_case(budget)
    with pytest.raises(TimeoutError) as error:
        await case._wait_for_rag_job("job-timeout", "repo-url", "main@abcdef1")
    assert clock.sleeps == sleeps
    assert case.rag_indexer_trigger.get_job_status.call_count == polls
    assert str(error.value) == (
        f"RAG indexing job job-timeout timed out for repo-url (main@abcdef1): "
        f"elapsed={budget:g}s, timeout={budget:g}s"
    )
    case.rag_indexer_trigger.cancel_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [15, 20])
async def test_late_success_is_rejected(clock, delay):
    """Synchronous IO time counts; success at or beyond the deadline is too late."""
    case = use_case(25)

    def slow_query(_job_id):
        """Simulate a status call crossing the deadline."""
        clock.now += delay
        return status("SUCCEEDED")

    case.rag_indexer_trigger.get_job_status.side_effect = slow_query
    with pytest.raises(TimeoutError, match="timeout=25s"):
        await case._wait_for_rag_job("slow", "repo", "main")
    assert case.rag_indexer_trigger.get_job_status.call_count == 1


@pytest.mark.asyncio
async def test_queue_states_and_query_time_consume_budget(clock):
    """Queued jobs and network latency are counted, rather than polling attempts."""
    case = use_case(25)

    def slow_queued_query(_job_id):
        """Simulate queue status taking four seconds to arrive."""
        clock.now += 4
        return status("RUNNABLE")

    case.rag_indexer_trigger.get_job_status.side_effect = slow_queued_query
    with pytest.raises(TimeoutError, match="elapsed=28s"):
        await case._wait_for_rag_job("queued", "repo", "main")
    assert clock.sleeps == [10, 10]


@pytest.mark.asyncio
async def test_full_and_delta_each_get_complete_budget(clock):
    """Two jobs may together exceed one budget while each finishes on time."""
    case = use_case(25)
    case.rag_indexer_trigger.trigger_full.return_value = "full-job"
    case.rag_indexer_trigger.trigger_delta.return_value = "delta-job"
    case.rag_indexer_trigger.get_job_status.side_effect = [
        status("RUNNING"),
        status("SUCCEEDED"),
    ] * 2
    await case._ensure_rag_index("repo", "main", "abcdef123", "full")
    assert clock.now == 1040
    assert [
        call.args[0] for call in case.rag_indexer_trigger.get_job_status.call_args_list
    ] == ["full-job", "full-job", "delta-job", "delta-job"]


@pytest.mark.asyncio
async def test_timeout_prevents_analysis(clock):
    """An expired initial index prevents analysis and notifications."""
    case = use_case(2.5)
    case.task_repository.get_task.return_value = SimpleNamespace(
        branch="main",
        repository_url="repo",
        commit_hash="abc",
        args={},
        source=module.TaskSource.GITHUB,
        mark_in_progress=MagicMock(),
    )
    with pytest.raises(TimeoutError):
        await case.execute("scan")
    case.agent.invoke.assert_not_called()
    case.notification_service.send_notifications.assert_not_called()


def test_commit_delta_remains_fire_and_forget():
    """Post-scan delta submits without polling even with a tiny budget."""
    case = use_case(0.5)
    case._trigger_delta_indexing("repo", "main", "abc")
    case.rag_indexer_trigger.trigger_delta.assert_called_once_with(
        "repo", "main", "abc"
    )
    case.rag_indexer_trigger.get_job_status.assert_not_called()

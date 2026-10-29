"""Coordinate Git RAG readiness, code analysis and result persistence."""

import asyncio
import json
import logging
import math
from datetime import datetime
from time import monotonic
from typing import Any, Dict

from code_analysis.domain.dto.result_dto import AnalysisStatus, ResultDto
from code_analysis.domain.entities.task_entity import Task, TaskSource, TaskStatus
from code_analysis.domain.notification_service import NotificationService
from code_analysis.domain.ports.ia_agent import AbstractAgent, AgentMessage
from code_analysis.domain.ports.rag_index_status_port import IRagIndexStatusPort
from code_analysis.domain.ports.task_repository import ITaskRepository
from rag_indexer_trigger.rag_indexer_batch_trigger import RagIndexerBatchTrigger

LOGGER = logging.getLogger(__name__)

_RAG_POLL_INTERVAL_S = 10
_SCAN_MODE_COMMIT = "commit"
_SCAN_MODE_FULL = "full"


class AnalyseCodeUseCase:
    """Use case para analizar el código de un repositorio de Git o Bitbucket.

    Args:
        task_repository (ITaskRepository): Repositorio de tareas.
        agent (AbstractAgent): Agente para analizar el código.
        content_template (str): Plantilla de contenido para el agente.
        notification_service (NotificationService): Servicio de notificaciones.
        rag_index_status (IRagIndexStatusPort): Consulta si el índice RAG existe.
        rag_indexer_trigger (RagIndexerBatchTrigger): Dispara jobs de indexación.
        rag_indexing_timeout_seconds: Presupuesto por job desde la tabla; None para CLI.
    """

    def __init__(
        self,
        task_repository: ITaskRepository,
        agent: AbstractAgent,
        notification_service: NotificationService,
        content_template: str,
        rag_index_status: IRagIndexStatusPort | None,
        rag_indexer_trigger: RagIndexerBatchTrigger | None,
        rag_indexing_timeout_seconds: float | None,
    ):
        """Inject collaborators and the scan's configuration snapshot."""
        self.task_repository = task_repository
        self.agent = agent
        self.content_template = content_template
        self.notification_service = notification_service
        self.rag_index_status = rag_index_status
        self.rag_indexer_trigger = rag_indexer_trigger
        self.rag_indexing_timeout_seconds = rag_indexing_timeout_seconds

    def _require_rag_timeout(self) -> float:
        """Reject missing/invalid Git budgets before submitting indexing jobs."""
        value = self.rag_indexing_timeout_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                "rag_indexing_timeout_seconds must be configured "
                "as finite positive seconds"
            )
        return value

    @staticmethod
    def _normalize_scan_mode(scan_mode: object) -> str:
        if scan_mode in (None, ""):
            return _SCAN_MODE_COMMIT
        if scan_mode not in {_SCAN_MODE_COMMIT, _SCAN_MODE_FULL}:
            raise ValueError("scan_mode must be one of: commit, full")
        return str(scan_mode)

    async def _wait_for_rag_job(self, job_id: str, repo_url: str, label: str) -> None:
        """Wait within a per-job monotonic budget without cancelling the Batch job."""
        timeout = self._require_rag_timeout()
        started = monotonic()
        deadline = started + timeout
        LOGGER.info("Waiting for indexing job %s (timeout=%gs)", job_id, timeout)
        while (remaining := deadline - monotonic()) > 0:
            await asyncio.sleep(min(_RAG_POLL_INTERVAL_S, remaining))
            if monotonic() >= deadline:
                break
            status = self.rag_indexer_trigger.get_job_status(job_id)
            now = monotonic()
            LOGGER.info(
                "Indexing job %s status: %s (elapsed=%gs, timeout=%gs)",
                job_id,
                status.status,
                now - started,
                timeout,
            )
            # A synchronous SDK call may return after the deadline: reject late results.
            if now >= deadline:
                break
            if status.is_succeeded:
                LOGGER.info("RAG indexing completed for %s (%s)", repo_url, label)
                return
            if status.is_failed:
                raise RuntimeError(
                    f"RAG indexing job {job_id} failed for {repo_url} ({label})"
                )

        raise TimeoutError(
            f"RAG indexing job {job_id} timed out for {repo_url} ({label}): "
            f"elapsed={monotonic() - started:g}s, timeout={timeout:g}s"
        )

    async def _ensure_branch_rag_index(self, repo_url: str, branch: str) -> None:
        """Ensure the RAG index exists for the branch.

        Blocks until the indexing job completes or raises on failure/timeout.
        """
        if self.rag_index_status is None or self.rag_indexer_trigger is None:
            raise ValueError("Git scans require RAG configuration")
        self._require_rag_timeout()
        if self.rag_index_status.is_indexed(repo_url, branch):
            LOGGER.info("RAG index already available for %s@%s", repo_url, branch)
            return

        LOGGER.info(
            "RAG index not found for %s@%s — triggering full indexing",
            repo_url,
            branch,
        )
        job_id = self.rag_indexer_trigger.trigger_full(repo_url, branch)
        LOGGER.info("Full indexing job submitted: %s", job_id)
        await self._wait_for_rag_job(job_id, repo_url, branch)

    async def _ensure_rag_index(
        self, repo_url: str, branch: str, commit_hash: str, scan_mode: str
    ) -> None:
        """Ensure RAG context is available, and fresh for full scans."""
        await self._ensure_branch_rag_index(repo_url, branch)

        if scan_mode != _SCAN_MODE_FULL:
            return

        if self.rag_index_status.is_commit_indexed(repo_url, branch, commit_hash):
            LOGGER.info(
                "RAG index already fresh for %s@%s (%s)",
                repo_url,
                branch,
                commit_hash[:7],
            )
            return

        LOGGER.info(
            "RAG index is stale for full scan %s@%s (%s) — triggering delta indexing",
            repo_url,
            branch,
            commit_hash[:7],
        )
        job_id = self.rag_indexer_trigger.trigger_delta(repo_url, branch, commit_hash)
        LOGGER.info("Delta indexing job submitted for full scan freshness: %s", job_id)
        await self._wait_for_rag_job(job_id, repo_url, f"{branch}@{commit_hash[:7]}")

    def _trigger_delta_indexing(
        self, repo_url: str, branch: str, commit_hash: str
    ) -> None:
        """Fire-and-forget delta indexing. Errors are logged but do not propagate."""
        try:
            if self.rag_index_status.is_commit_indexed(repo_url, branch, commit_hash):
                LOGGER.info(
                    "Commit %s already indexed for %s@%s — skipping delta trigger",
                    commit_hash[:7],
                    repo_url,
                    branch,
                )
                return
        except Exception as exc:
            LOGGER.error(
                "Failed to check commit index status for %s@%s: %s — "
                "skipping delta trigger",
                repo_url,
                commit_hash[:7],
                exc,
            )
            return

        try:
            job_id = self.rag_indexer_trigger.trigger_delta(
                repo_url, branch, commit_hash
            )
            LOGGER.info(
                "Delta indexing job submitted for %s@%s: %s",
                repo_url,
                commit_hash[:7],
                job_id,
            )
        except Exception as exc:
            LOGGER.error(
                "Failed to trigger delta indexing for %s@%s: %s",
                repo_url,
                commit_hash[:7],
                exc,
            )

    def __sanitize_content_response(self, content: str) -> str:
        # Check if content is wrapped in ```json and ```
        content_stripped = content.strip()
        if content_stripped.startswith("```json"):
            # Remove the first ```json match
            content = content_stripped.replace("```json", "", 1).strip()
            # Verify it ends with ``` before removing (safety check)
            if content.endswith("```"):
                # Remove the last ``` from the end (last 3 characters)
                content = content[:-3].strip()
            return content
        return content

    @staticmethod
    def _log_failure(task_id: str, result: Dict[str, Any]) -> None:
        """Explain in the logs why a scan ended FAILED."""
        error = result.get("error")
        incomplete = result.get("incomplete") or {}
        coverage_errors = (result.get("coverage") or {}).get("errors") or []
        if error:
            LOGGER.error(
                "Task %s FAILED because of an infrastructure error: %s", task_id, error
            )
        else:
            LOGGER.warning(
                "Task %s FAILED: %d finding(s) with CRITICAL/HIGH severity",
                task_id,
                result.get("issues_count", 0),
            )
        if incomplete:
            LOGGER.warning(
                "Task %s incomplete: %s (files: %s)",
                task_id,
                incomplete.get("message"),
                ", ".join(incomplete.get("files_not_fully_analyzed", [])),
            )
        for coverage_error in coverage_errors:
            LOGGER.warning("Task %s expert error: %s", task_id, coverage_error)

    async def execute(self, task_id: str) -> Task:
        LOGGER.info("Executing analyse code use case with task id %s", task_id)
        task = self.task_repository.get_task(task_id)

        if not task.branch:
            raise ValueError(
                f"Task {task_id} is missing required field 'branch'. "
                "Ensure the trigger API sends TITVO_BRANCH."
            )

        task.mark_in_progress()
        self.task_repository.update_task(task)
        LOGGER.debug("Marking task %s as in progress", task_id)

        scan_mode = self._normalize_scan_mode(task.args.get("scan_mode"))
        if task.source != TaskSource.CLI:
            await self._ensure_rag_index(
                task.repository_url, task.branch, task.commit_hash, scan_mode
            )

        analysis_args = {**task.args, "scan_mode": scan_mode}
        content_args = ""
        for key, value in analysis_args.items():
            if key == "repository_url":
                LOGGER.debug("Skipping repository url: %s", value)
                continue
            content_args += f"- {key}: {value}\n"
            LOGGER.debug("Adding argument: %s: %s", key, value)

        rag_context = (
            f"Note: The codebase for branch `{task.branch}` is indexed as background "
            "context. The selected analysis files are retrieved via MCP tools."
        )

        if task.source == TaskSource.CLI:
            rag_context = (
                "CLI snapshot: RAG disabled; analyze uploaded working-tree files only."
            )

        message = AgentMessage(
            role="user",
            content=self.content_template.format(
                repository_url=task.repository_url,
                commit_hash=task.commit_hash,
                branch=task.branch,
                rag_context=rag_context,
                args=content_args,
                # Files retrieved internally by LangGraph MCP node
                files_content="",
            ),
            metadata={
                "repository_url": task.repository_url,
                "commit_hash": task.commit_hash,
                "branch": task.branch,
                "scan_mode": scan_mode,
                "scan_ref": task.branch,
                "extra_args": analysis_args,
                "created_at": created.isoformat()
                if isinstance(created := getattr(task, "created_at", None), datetime)
                else created
                if isinstance(created, str)
                else None,
            },
        )
        LOGGER.debug("Sending message to agent: %s", message.content)
        agent_response = await self.agent.invoke(message)
        LOGGER.debug("Agent response: %s", agent_response.content)
        if task.source != TaskSource.CLI:
            self._trigger_delta_indexing(
                task.repository_url, task.branch, task.commit_hash
            )
        agent_response.content = self.__sanitize_content_response(
            agent_response.content
        )
        LOGGER.debug("Sanitized agent response: %s", agent_response.content)
        result = json.loads(agent_response.content)
        LOGGER.info("Result: %s", result)
        result_dto = ResultDto(
            **{
                **{
                    key: value
                    for key, value in result.items()
                    if key
                    in {"status", "scaned_files", "issues", "error", "incomplete"}
                },
                "source": task.source.value,
                "args": task.args,
                "commit_hash": task.commit_hash,
            }
        )
        notifications_results = self.notification_service.send_notifications(result_dto)
        # Keep counts in DynamoDB while the existing HTML report holds findings.
        result["issues_count"] = len(result.get("issues", []))
        # Remove issues from result if exists
        if "issues" in result:
            result.pop("issues")
        status = result.get("status")
        LOGGER.info("Status: %s", status)
        result = {**result, **notifications_results}
        try:
            if status == AnalysisStatus.COMPLETED.value:
                task.mark_completed(result, result.get("scaned_files"))
                LOGGER.info("Marking task %s as completed", task_id)
            elif status == AnalysisStatus.FAILED.value:
                task.mark_failed(result, result.get("scaned_files"))
                self._log_failure(task_id, result)
            elif status == AnalysisStatus.WARNING.value:
                LOGGER.error("Marking task %s as error", task_id)
                task.mark_completed(result, result.get("scaned_files"))
            else:
                raise ValueError(f"Invalid status: {status}")
        except Exception as e:
            LOGGER.error("Error marking task %s as %s: %s", task_id, status, e)
            task.mark_error()
        return self.task_repository.update_task(task)


def exit_code_for_task(task: Task) -> int:
    """Process exit code for the Batch job once the task has been persisted.

    Returns 1 only when the scan could not run (infrastructure error or an
    unexpected exception while persisting). A FAILED task caused by findings
    is a successful scan and exits with 0.
    """
    if task.status == TaskStatus.ERROR:
        return 1
    if task.status == TaskStatus.FAILED and (task.result or {}).get("error"):
        return 1
    return 0

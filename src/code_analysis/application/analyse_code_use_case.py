import asyncio
import json
import logging

from code_analysis.domain.dto.result_dto import AnalysisStatus, ResultDto
from code_analysis.domain.entities.task_entity import Task
from code_analysis.domain.notification_service import NotificationService
from code_analysis.domain.ports.ia_agent import AbstractAgent, AgentMessage
from code_analysis.domain.ports.rag_index_status_port import IRagIndexStatusPort
from code_analysis.domain.ports.task_repository import ITaskRepository
from rag_indexer_trigger.rag_indexer_batch_trigger import RagIndexerBatchTrigger

LOGGER = logging.getLogger(__name__)

_RAG_POLL_INTERVAL_S = 10
_RAG_MAX_ATTEMPTS = 60
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
    """

    def __init__(
        self,
        task_repository: ITaskRepository,
        agent: AbstractAgent,
        notification_service: NotificationService,
        content_template: str,
        rag_index_status: IRagIndexStatusPort,
        rag_indexer_trigger: RagIndexerBatchTrigger,
    ):
        self.task_repository = task_repository
        self.agent = agent
        self.content_template = content_template
        self.notification_service = notification_service
        self.rag_index_status = rag_index_status
        self.rag_indexer_trigger = rag_indexer_trigger

    @staticmethod
    def _normalize_scan_mode(scan_mode: object) -> str:
        if scan_mode in (None, ""):
            return _SCAN_MODE_COMMIT
        if scan_mode not in {_SCAN_MODE_COMMIT, _SCAN_MODE_FULL}:
            raise ValueError("scan_mode must be one of: commit, full")
        return str(scan_mode)

    async def _wait_for_rag_job(self, job_id: str, repo_url: str, label: str) -> None:
        for attempt in range(1, _RAG_MAX_ATTEMPTS + 1):
            await asyncio.sleep(_RAG_POLL_INTERVAL_S)
            status = self.rag_indexer_trigger.get_job_status(job_id)
            LOGGER.info(
                "Indexing job %s status: %s (attempt %d/%d)",
                job_id,
                status.status,
                attempt,
                _RAG_MAX_ATTEMPTS,
            )
            if status.is_succeeded:
                LOGGER.info("RAG indexing completed for %s (%s)", repo_url, label)
                return
            if status.is_failed:
                raise RuntimeError(
                    f"RAG indexing job {job_id} failed for {repo_url} ({label})"
                )

        raise TimeoutError(
            f"RAG indexing timed out for {repo_url} ({label}) "
            f"after {_RAG_MAX_ATTEMPTS * _RAG_POLL_INTERVAL_S}s"
        )

    async def _ensure_branch_rag_index(self, repo_url: str, branch: str) -> None:
        """Ensure the RAG index exists for the branch.

        Blocks until the indexing job completes or raises on failure/timeout.
        """
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
            },
        )
        LOGGER.debug("Sending message to agent: %s", message.content)
        agent_response = await self.agent.invoke(message)
        LOGGER.debug("Agent response: %s", agent_response.content)
        self._trigger_delta_indexing(task.repository_url, task.branch, task.commit_hash)
        agent_response.content = self.__sanitize_content_response(
            agent_response.content
        )
        LOGGER.debug("Sanitized agent response: %s", agent_response.content)
        result = json.loads(agent_response.content)
        LOGGER.info("Result: %s", result)
        result_dto = ResultDto(
            **{
                **result,
                "source": task.source.value,
                "args": task.args,
                "commit_hash": task.commit_hash,
            }
        )
        notifications_results = self.notification_service.send_notifications(result_dto)
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
                LOGGER.warning("Marking task %s as failed", task_id)
            elif status == AnalysisStatus.WARNING.value:
                LOGGER.error("Marking task %s as error", task_id)
                task.mark_completed(result, result.get("scaned_files"))
            else:
                raise ValueError(f"Invalid status: {status}")
        except Exception as e:
            LOGGER.error("Error marking task %s as %s: %s", task_id, status, e)
            task.mark_error()
        return self.task_repository.update_task(task)

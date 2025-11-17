import json
import logging
from enum import Enum

from code_analysis.domain.entities.task_entity import Task
from code_analysis.domain.ports.ia_agent import AbstractAgent, AgentMessage
from code_analysis.domain.ports.task_repository import ITaskRepository

LOGGER = logging.getLogger(__name__)


class AnalysisStatus(Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WARNING = "WARNING"


class AnalyseCodeUseCase:
    """Use case para analizar el código de un repositorio de Git o Bitbucket.

    Args:
        task_repository (ITaskRepository): Repositorio de tareas.
        agent (AbstractAgent): Agente para analizar el código.
        content_template (str): Plantilla de contenido para el agente.
    """

    def __init__(
        self,
        task_repository: ITaskRepository,
        agent: AbstractAgent,
        content_template: str,
    ):
        self.task_repository = task_repository
        self.agent = agent
        self.content_template = content_template

    async def execute(self, task_id: str) -> Task:
        LOGGER.info("Executing analyse code use case with task id %s", task_id)
        task = self.task_repository.get_task(task_id)
        task.mark_in_progress()
        self.task_repository.update_task(task)
        LOGGER.debug("Marking task %s as in progress", task_id)
        content_args = ""
        for key, value in task.args.items():
            if key == "repository_url":
                LOGGER.debug("Skipping repository url: %s", value)
                continue
            content_args += f"- {key}: {value}\n"
            LOGGER.debug("Adding argument: %s: %s", key, value)
        message = AgentMessage(
            role="user",
            content=self.content_template.format(
                repository_url=task.repository_url,
                commit_hash=task.commit_hash,
                args=content_args,
            ),
        )
        LOGGER.debug("Sending message to agent: %s", message.content)
        agent_response = await self.agent.invoke(message)
        LOGGER.debug("Agent response: %s", agent_response.content)
        result = json.loads(agent_response.content)
        status = result.get("status")
        LOGGER.debug("Status: %s", status)
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

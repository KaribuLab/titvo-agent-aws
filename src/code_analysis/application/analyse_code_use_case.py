import logging

from langchain_core.output_parsers import json

from code_analysis.domain.entities.task_entity import Task, TaskStatus
from code_analysis.domain.ports.ia_agent import AbstractAgent, AgentMessage
from code_analysis.domain.ports.task_repository import ITaskRepository

LOGGER = logging.getLogger(__name__)


class AnalyseCodeUseCase:
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
        message = AgentMessage(
            role="user",
            content=self.content_template.format(
                repository_url=task.repository_url, commit_hash=task.commit_hash
            ),
        )
        LOGGER.debug("Sending message to agent: %s", message.content)
        agent_response = await self.agent.invoke(message)
        LOGGER.debug("Agent response: %s", agent_response.content)
        result = json.loads(agent_response.content)
        status = result.get("status")
        LOGGER.debug("Status: %s", status)
        if status == TaskStatus.COMPLETED:
            task.mark_completed(result, result.get("scaned_files"))
            LOGGER.info("Marking task %s as completed", task_id)
        elif status == TaskStatus.FAILED:
            task.mark_failed(result, result.get("scaned_files"))
            LOGGER.warning("Marking task %s as failed", task_id)
        elif status == TaskStatus.ERROR:
            task.mark_error()
            LOGGER.error("Marking task %s as error", task_id)
        else:
            raise ValueError(f"Invalid status: {status}")
        return self.task_repository.update_task(task)

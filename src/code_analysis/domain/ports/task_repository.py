from abc import ABC, abstractmethod

from code_analysis.domain.entities.task_entity import Task


class ITaskRepository(ABC):
    @abstractmethod
    def get_task(self, task_id: str) -> Task:
        pass

    @abstractmethod
    def update_task(self, task: Task) -> Task:
        pass

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict


class TaskSource(Enum):
    GITHUB = "github"
    BITBUCKET = "bitbucket"
    CLI = "cli"


class TaskStatus(Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ERROR = "ERROR"


@dataclass
class Task:
    id: str
    result: Dict[str, Any]
    args: Dict[str, Any]
    hint_id: str
    scaned_files: int
    created_at: datetime
    updated_at: datetime
    status: TaskStatus
    source: TaskSource

    @property
    def repository_url(self) -> str:
        # FIXME: Desde ahora en adelante se debe usar el repository_url en lugar de los
        # otros campos
        if self.source == TaskSource.GITHUB:
            github_repo_name = self.args.get("github_repo_name")
            if github_repo_name is None:
                raise ValueError("Github repo name is required")
            return f"https://github.com/{github_repo_name}"
        elif self.source == TaskSource.BITBUCKET:
            bitbucket_workspace = self.args.get("bitbucket_workspace")
            if bitbucket_workspace is None:
                raise ValueError("Bitbucket workspace is required")
            bitbucket_repo_slug = self.args.get("bitbucket_repo_slug")
            if bitbucket_repo_slug is None:
                raise ValueError("Bitbucket repo slug is required")
            return f"https://bitbucket.org/{bitbucket_workspace}/{bitbucket_repo_slug}"
        else:
            repository_url = self.args.get("repository_url")
            if repository_url is None:
                raise ValueError("Repository URL is required")
            return repository_url

    @property
    def commit_hash(self) -> str:
        # FIXME: Desde ahora en adelante se debe usar el commit_hash en lugar de los
        # otros campos
        if self.source == TaskSource.GITHUB:
            github_commit_sha = self.args.get("github_commit_sha")
            if github_commit_sha is None:
                raise ValueError("Github commit SHA must be a string and not None")
            return github_commit_sha
        elif self.source == TaskSource.BITBUCKET:
            bitbucket_commit = self.args.get("bitbucket_commit")
            if bitbucket_commit is None:
                raise ValueError("Bitbucket commit must be a string and not None")
            return bitbucket_commit
        else:
            commit_hash = self.args.get("commit_hash")
            if commit_hash is None:
                raise ValueError("Commit hash must be a string and not None")
            return commit_hash

    def mark_in_progress(self):
        self.status = TaskStatus.IN_PROGRESS
        self.updated_at = datetime.now()

    def mark_completed(self, result: Dict[str, Any], scaned_files: int):
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.scaned_files = scaned_files
        self.updated_at = datetime.now()

    def mark_failed(self, result: Dict[str, Any], scaned_files: int):
        self.status = TaskStatus.FAILED
        self.result = result
        self.scaned_files = scaned_files
        self.updated_at = datetime.now()

    def mark_error(self):
        self.status = TaskStatus.ERROR
        self.updated_at = datetime.now()

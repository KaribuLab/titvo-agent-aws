"""RagIndexerBatchTrigger — triggers full and delta RAG indexing jobs via Batch."""

import logging
import os
import uuid

from rag_indexer_trigger.batch_service import BatchService, JobStatusResponse

LOGGER = logging.getLogger(__name__)


class RagIndexerBatchTrigger:
    """Submits full and delta indexing jobs for the rag-indexer service.

    Full indexing: indexes the entire branch (no commit_sha).
    Delta indexing: indexes only the changes introduced by a specific commit.
    """

    def __init__(
        self,
        batch_service: BatchService,
        job_queue: str,
        job_definition: str,
        config_table_name: str,
        encryption_key_name: str,
        aws_stage: str = "",
        aws_endpoint: str = "",
        log_level: str = "INFO",
    ):
        self._batch_service = batch_service
        self._job_queue = job_queue
        self._job_definition = job_definition
        self._config_table_name = config_table_name
        self._encryption_key_name = encryption_key_name
        self._aws_stage = aws_stage
        self._aws_endpoint = aws_endpoint
        self._log_level = log_level

    def trigger_full(self, repo_url: str, branch: str) -> str:
        """Trigger full indexing for a branch. Returns the batch job ID."""
        job_name = f"rag-indexer-full-{uuid.uuid4().hex[:8]}"
        environment = self._build_environment(repo_url=repo_url, branch=branch)
        LOGGER.info(
            "Triggering full RAG indexing: repo=%s, branch=%s, job=%s",
            repo_url,
            branch,
            job_name,
        )
        return self._batch_service.submit_job(
            job_name=job_name,
            job_queue=self._job_queue,
            job_definition=self._job_definition,
            environment=environment,
        )

    def trigger_delta(self, repo_url: str, branch: str, commit_sha: str) -> str:
        """Trigger delta indexing for a specific commit. Returns the batch job ID."""
        job_name = f"rag-indexer-delta-{uuid.uuid4().hex[:8]}"
        environment = self._build_environment(
            repo_url=repo_url, branch=branch, commit_sha=commit_sha
        )
        LOGGER.info(
            "Triggering delta RAG indexing: repo=%s, branch=%s, commit=%s, job=%s",
            repo_url,
            branch,
            commit_sha[:7] if commit_sha else "",
            job_name,
        )
        return self._batch_service.submit_job(
            job_name=job_name,
            job_queue=self._job_queue,
            job_definition=self._job_definition,
            environment=environment,
        )

    def get_job_status(self, job_id: str) -> JobStatusResponse:
        """Delegate status check to the underlying BatchService."""
        return self._batch_service.get_job_status(job_id)

    def _build_environment(
        self,
        repo_url: str,
        branch: str,
        commit_sha: str = "",
    ) -> list:
        env = [
            {"name": "TITVO_REPO_URL", "value": repo_url},
            {"name": "TITVO_BRANCH", "value": branch},
        ]
        if commit_sha:
            env.append({"name": "TITVO_COMMIT_SHA", "value": commit_sha})
        if self._aws_stage == "localstack":
            env.append({"name": "AWS_STAGE", "value": self._aws_stage})
            env.append(
                {
                    "name": "TITVO_DYNAMO_CONFIGURATION_TABLE_NAME",
                    "value": self._config_table_name,
                }
            )
            env.append(
                {
                    "name": "TITVO_ENCRYPTION_KEY_NAME",
                    "value": self._encryption_key_name,
                }
            )
            env.append({"name": "TITVO_LOG_LEVEL", "value": self._log_level})
            if self._aws_endpoint:
                env.append({"name": "AWS_ENDPOINT", "value": self._aws_endpoint})
        return env


def create_rag_indexer_batch_trigger() -> RagIndexerBatchTrigger:
    """Factory that reads configuration from environment variables."""
    from rag_indexer_trigger.batch_service import create_batch_service

    aws_stage = os.getenv("AWS_STAGE", "")
    is_localstack = aws_stage == "localstack"

    if is_localstack:
        job_queue = ""
        job_definition = ""
    else:
        job_queue = os.getenv("TITVO_RAG_INDEXER_JOB_QUEUE")
        if not job_queue:
            raise ValueError("TITVO_RAG_INDEXER_JOB_QUEUE is not set")
        job_definition = os.getenv("TITVO_RAG_INDEXER_JOB_DEFINITION")
        if not job_definition:
            raise ValueError("TITVO_RAG_INDEXER_JOB_DEFINITION is not set")

    config_table_name = os.getenv("TITVO_DYNAMO_CONFIGURATION_TABLE_NAME")
    if not config_table_name:
        raise ValueError("TITVO_DYNAMO_CONFIGURATION_TABLE_NAME is not set")

    encryption_key_name = os.getenv("TITVO_ENCRYPTION_KEY_NAME")
    if not encryption_key_name:
        raise ValueError("TITVO_ENCRYPTION_KEY_NAME is not set")

    aws_endpoint = os.getenv("AWS_ENDPOINT", "")
    log_level = os.getenv("TITVO_LOG_LEVEL", "INFO")

    batch_service = create_batch_service(aws_stage=aws_stage)

    return RagIndexerBatchTrigger(
        batch_service=batch_service,
        job_queue=job_queue,
        job_definition=job_definition,
        config_table_name=config_table_name,
        encryption_key_name=encryption_key_name,
        aws_stage=aws_stage,
        aws_endpoint=aws_endpoint,
        log_level=log_level,
    )

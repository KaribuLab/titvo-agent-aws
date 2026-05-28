"""BatchService — submits and queries AWS Batch jobs.

Supports two modes:
- localstack: HTTP calls to the batch-runner sidecar service.
- AWS: native boto3 Batch client.

The HTTP batch-runner API mirrors the TypeScript batch.service.ts implementation:
  POST /run-batch  — start a Docker container job
  POST /get-job-status — get the status of a job by jobId
"""
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

import boto3

LOGGER = logging.getLogger(__name__)

_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"
_RUNNING_STATUSES = {"SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"}


@dataclass
class JobStatusResponse:
    status: str
    is_failed: bool

    @property
    def is_terminal(self) -> bool:
        return self.status in (_SUCCEEDED, _FAILED)

    @property
    def is_succeeded(self) -> bool:
        return self.status == _SUCCEEDED


class BatchService:
    """Submits and polls AWS Batch (or local batch-runner) jobs."""

    def __init__(
        self,
        batch_client=None,
        batch_runner_url: Optional[str] = None,
    ):
        self._client = batch_client
        self._runner_url = batch_runner_url

    def submit_job(
        self,
        job_name: str,
        job_queue: str,
        job_definition: str,
        environment: List[Dict[str, str]],
        image_name: str = "titvo/rag-indexer",
        container_name: str = "titvo-rag-indexer-local",
        network_mode: str = "titvo-dev_localstack",
    ) -> str:
        """Submit a batch job and return the job ID."""
        if self._runner_url is not None:
            return self._submit_docker_job(
                environment=environment,
                image_name=image_name,
                container_name=container_name,
                network_mode=network_mode,
            )
        if self._client is not None:
            return self._submit_aws_batch_job(
                job_name=job_name,
                job_queue=job_queue,
                job_definition=job_definition,
                environment=environment,
            )
        raise RuntimeError("Neither batch-runner nor AWS Batch client is configured")

    def get_job_status(self, job_id: str) -> JobStatusResponse:
        """Return the current status of a submitted job."""
        if self._runner_url is not None:
            return self._get_docker_job_status(job_id)
        if self._client is not None:
            return self._get_aws_batch_job_status(job_id)
        raise RuntimeError("Neither batch-runner nor AWS Batch client is configured")

    # --- HTTP (batch-runner) helpers ---

    def _submit_docker_job(
        self,
        environment: List[Dict[str, str]],
        image_name: str,
        container_name: str,
        network_mode: str,
    ) -> str:
        env_array = [f"{e['name']}={e['value']}" for e in environment]
        payload = json.dumps(
            {
                "containerName": container_name,
                "environmentVariables": env_array,
                "imageName": image_name,
                "networkMode": network_mode,
            }
        ).encode()
        LOGGER.info("Submitting Docker job via batch-runner at %s: %s", self._runner_url, container_name)
        data = self._http_post(f"{self._runner_url}/run-batch", payload)
        job_id: str = data.get("jobId", "")
        LOGGER.info("Docker job submitted, jobId=%s", job_id)
        return job_id

    def _get_docker_job_status(self, job_id: str) -> JobStatusResponse:
        payload = json.dumps({"jobId": job_id}).encode()
        data = self._http_post(f"{self._runner_url}/get-job-status", payload)
        status: str = data.get("status", _FAILED)
        return JobStatusResponse(status=status, is_failed=status == _FAILED)

    # --- AWS Batch helpers ---

    def _submit_aws_batch_job(
        self,
        job_name: str,
        job_queue: str,
        job_definition: str,
        environment: List[Dict[str, str]],
    ) -> str:
        LOGGER.info(
            "Submitting AWS Batch job: name=%s, queue=%s, definition=%s",
            job_name,
            job_queue,
            job_definition,
        )
        response = self._client.submit_job(
            jobName=job_name,
            jobQueue=job_queue,
            jobDefinition=job_definition,
            containerOverrides={"environment": environment},
        )
        job_id: str = response["jobId"]
        LOGGER.info("AWS Batch job submitted, jobId=%s", job_id)
        return job_id

    def _get_aws_batch_job_status(self, job_id: str) -> JobStatusResponse:
        response = self._client.describe_jobs(jobs=[job_id])
        jobs = response.get("jobs", [])
        if not jobs:
            raise RuntimeError(f"describe_jobs returned no results for jobId={job_id}")
        status: str = jobs[0].get("status", _FAILED)
        return JobStatusResponse(status=status, is_failed=status == _FAILED)

    # --- shared HTTP utility ---

    def _http_post(self, url: str, body: bytes) -> dict:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"batch-runner returned HTTP {exc.code} for {url}"
            ) from exc


def create_batch_service(
    aws_stage: Optional[str] = None,
    batch_runner_url: Optional[str] = None,
) -> BatchService:
    """Factory that selects the appropriate BatchService implementation.

    - localstack: uses the HTTP batch-runner (URL from argument or
      TITVO_BATCH_RUNNER_URL env var, defaulting to http://rag-indexer:3002).
    - AWS: uses native boto3 Batch client.
    """
    stage = aws_stage or os.getenv("AWS_STAGE", "")
    if stage == "localstack":
        runner_url = (
            batch_runner_url
            or os.getenv("TITVO_BATCH_RUNNER_URL")
            or "http://rag-indexer:3002"
        )
        LOGGER.info(
            "Using batch-runner at %s for localstack (hostname: %s)",
            runner_url,
            runner_url.replace("http://", "").replace("https://", "").split(":")[0],
        )
        return BatchService(batch_runner_url=runner_url)

    aws_endpoint = os.getenv("AWS_ENDPOINT")
    if aws_endpoint:
        client = boto3.client("batch", endpoint_url=aws_endpoint)
    else:
        client = boto3.client("batch")
    return BatchService(batch_client=client)

"""S3-backed adapter for checking RAG index availability.

Checks existence of {repo_path}/branches/{branch}/latest/meta.json
using the same S3 path convention as the rag-indexer's S3ArtifactStoreAdapter.
"""
import logging
import os
import re
from typing import Any

import botocore.exceptions

from code_analysis.domain.ports.rag_index_status_port import IRagIndexStatusPort

LOGGER = logging.getLogger(__name__)


class S3RagIndexStatusAdapter(IRagIndexStatusPort):
    def __init__(self, s3_client: Any, bucket_name: str):
        self._s3 = s3_client
        self._bucket = bucket_name

    def is_indexed(self, repository_url: str, branch: str) -> bool:
        """Return True if latest/meta.json exists for this repository+branch."""
        repo_path = self._build_repo_path(repository_url)
        key = f"{repo_path}/branches/{branch}/latest/meta.json"
        return self._object_exists(key, repository_url, branch, "RAG index")

    def is_commit_indexed(
        self, repository_url: str, branch: str, commit_sha: str
    ) -> bool:
        """Return True if meta.json exists for this specific commit."""
        repo_path = self._build_repo_path(repository_url)
        key = f"{repo_path}/branches/{branch}/{commit_sha}/meta.json"
        return self._object_exists(
            key,
            repository_url,
            f"{branch}@{commit_sha[:7]}",
            "RAG commit index",
        )

    def _object_exists(
        self, key: str, repository_url: str, label: str, resource: str
    ) -> bool:
        LOGGER.debug("Checking %s: s3://%s/%s", resource, self._bucket, key)
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            LOGGER.info("%s found for %s (%s)", resource, repository_url, label)
            return True
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                LOGGER.info("%s not found for %s (%s)", resource, repository_url, label)
                return False
            LOGGER.error("S3 error checking %s: %s", resource, exc)
            raise

    @staticmethod
    def _build_repo_path(repository_url: str) -> str:
        url = re.sub(r"^https?://", "", repository_url)
        url = url.rstrip("/").replace(".git", "")
        return url


def create_s3_rag_index_status_adapter() -> S3RagIndexStatusAdapter:
    """Factory that reads bucket name from TITVO_RAG_INDEXER_BUCKET env var."""
    import boto3

    bucket_name = os.getenv("TITVO_RAG_INDEXER_BUCKET")
    if not bucket_name:
        raise ValueError("TITVO_RAG_INDEXER_BUCKET is not set")

    aws_endpoint = os.getenv("AWS_ENDPOINT")
    if aws_endpoint:
        s3_client = boto3.client("s3", endpoint_url=aws_endpoint)
    else:
        s3_client = boto3.client("s3")

    return S3RagIndexStatusAdapter(s3_client=s3_client, bucket_name=bucket_name)

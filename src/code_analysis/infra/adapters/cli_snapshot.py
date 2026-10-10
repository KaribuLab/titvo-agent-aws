"""Read CLI tar snapshots from S3 using the existing batch_id DynamoDB contract.

Archives are read in memory, never extracted onto the container filesystem.
An optional manifest verifies that every selected UTF-8 file arrived unchanged.
"""

import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath
from typing import Any

MANIFEST_PATH = ".titvo-manifest.json"
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def read_snapshot(archive: bytes) -> list[dict[str, str]]:
    """Validate paths, sizes and hashes before returning any snapshot files."""
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if member.isdir():
                continue
            if not member.isfile() or not path.parts:
                raise ValueError(f"Unsupported archive entry: {member.name}")
            name = path.as_posix()
            if name in files:
                raise ValueError(f"Duplicate archive path: {name}")
            total += member.size
            if total > MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot exceeds 100 MiB uncompressed")
            stream = tar.extractfile(member)
            assert stream is not None
            files[name] = stream.read()
    manifest_bytes = files.pop(MANIFEST_PATH, None)
    if manifest_bytes is not None:
        manifest = json.loads(manifest_bytes)
        entries = manifest["files"]
        expected = {entry["path"]: entry for entry in entries}
        if len(expected) != len(entries) or set(expected) != set(files):
            raise ValueError("Snapshot does not match manifest file list")
        for path, data in files.items():
            entry = expected[path]
            if (
                len(data) != entry["size"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise ValueError(f"Snapshot integrity mismatch: {path}")
    if not files:
        raise ValueError("CLI snapshot contains no files")
    return [
        {"path": path, "content": data.decode("utf-8")}
        for path, data in sorted(files.items())
    ]


class CliSnapshotRepository:
    """Download registered CLI packages, including all DynamoDB query pages."""

    def __init__(self, s3: Any, dynamodb: Any, bucket: str, table: str):
        self.s3 = s3
        self.dynamodb = dynamodb
        self.bucket = bucket
        self.table = table

    def get_files(self, batch_id: str) -> list[dict[str, str]]:
        """Reject missing packages and duplicate paths instead of scanning partially."""
        if not batch_id:
            raise ValueError("CLI batch_id is required")
        query = {
            "TableName": self.table,
            "IndexName": "batch_id_gsi",
            "KeyConditionExpression": "batch_id = :batch_id",
            "ExpressionAttributeValues": {":batch_id": {"S": batch_id}},
        }
        keys = []
        while True:
            page = self.dynamodb.query(**query)
            keys.extend(item["file_key"]["S"] for item in page["Items"])
            if not page.get("LastEvaluatedKey"):
                break
            query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        if not keys:
            raise ValueError(f"No CLI files registered for batch {batch_id}")
        files = {}
        for key in sorted(keys):
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
            try:
                archive = body.read(MAX_SNAPSHOT_BYTES + 1)
            finally:
                body.close()
            if len(archive) > MAX_SNAPSHOT_BYTES:
                raise ValueError("CLI package exceeds 100 MiB compressed")
            for file in read_snapshot(archive):
                if file["path"] in files:
                    raise ValueError(f"Duplicate CLI file: {file['path']}")
                files[file["path"]] = file
        return [files[path] for path in sorted(files)]

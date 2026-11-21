"""Read CLI tar snapshots from S3 using the existing batch_id DynamoDB contract.

Archives are read in memory, never extracted onto the container filesystem.
An optional manifest verifies that every selected UTF-8 file arrived unchanged.

Batch objects are typed by key: ``*.tar.gz`` / snapshot packages are archives;
``scan-manifest.json`` is JSON metadata and MUST NOT be opened as tar.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

MANIFEST_PATH = ".titvo-manifest.json"
SCAN_MANIFEST_SUFFIX = "scan-manifest.json"
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliBatchSnapshot:
    """Analyzable files plus optional sidecar scan-manifest metadata."""

    files: list[dict[str, str]]
    scan_manifest: dict[str, Any] | None


def read_snapshot(archive: bytes) -> list[dict[str, str]]:
    """Validate a package and return only analyzable UTF-8 text files."""
    return _text_files(_read_snapshot_entries(archive))


def _read_snapshot_entries(archive: bytes) -> dict[str, bytes]:
    """Validate all entries, including files later excluded from analysis."""
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
    return files


def _text_files(files: dict[str, bytes]) -> list[dict[str, str]]:
    """Exclude NUL-containing or non-UTF-8 content without altering valid text."""
    result = []
    for path, data in sorted(files.items()):
        if b"\x00" in data:
            LOGGER.warning("Skipping CLI file %r: binary content (NUL byte)", path)
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            LOGGER.warning("Skipping CLI file %r: content is not valid UTF-8", path)
            continue
        result.append({"path": path, "content": content})
    return result


def _is_scan_manifest_key(key: str) -> bool:
    return key.endswith(SCAN_MANIFEST_SUFFIX)


def _is_tar_key(key: str) -> bool:
    return key.endswith(".tar.gz")


def _parse_scan_manifest(body: bytes) -> dict[str, Any]:
    """Parse sidecar JSON; reject non-objects so callers never treat it as tar."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid scan-manifest.json: not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid scan-manifest.json: expected a JSON object")
    return payload


def _apply_focus(
    files: list[dict[str, str]], scan_manifest: dict[str, Any] | None
) -> list[dict[str, str]]:
    """Limit analysis to focus_files when non-empty; empty focus = all analyzable.

    Deleted focus paths may be absent from the tar inventory; they are included
    with empty content so experts can still reason about the path.
    """
    if not scan_manifest:
        return files
    focus = scan_manifest.get("focus_files") or []
    if not focus:
        return files
    by_path = {item["path"]: item for item in files}
    result: list[dict[str, str]] = []
    for path in focus:
        if path in by_path:
            result.append(by_path[path])
        else:
            result.append({"path": path, "content": ""})
    return result


class CliSnapshotRepository:
    """Download registered CLI packages, including all DynamoDB query pages."""

    def __init__(self, s3: Any, dynamodb: Any, bucket: str, table: str):
        self.s3 = s3
        self.dynamodb = dynamodb
        self.bucket = bucket
        self.table = table

    def get_batch(self, batch_id: str) -> CliBatchSnapshot:
        """Reject missing packages and duplicate paths instead of scanning partially.

        Object keys ending with ``scan-manifest.json`` are parsed as JSON sidecar
        metadata. Keys ending with ``.tar.gz`` are validated as snapshot archives.
        """
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

        files: dict[str, dict[str, str]] = {}
        seen_paths: set[str] = set()
        scan_manifest: dict[str, Any] | None = None

        for key in sorted(keys):
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
            try:
                payload = body.read(MAX_SNAPSHOT_BYTES + 1)
            finally:
                body.close()
            if len(payload) > MAX_SNAPSHOT_BYTES:
                raise ValueError("CLI package exceeds 100 MiB compressed")

            if _is_scan_manifest_key(key):
                if scan_manifest is not None:
                    raise ValueError("Duplicate scan-manifest.json in CLI batch")
                scan_manifest = _parse_scan_manifest(payload)
                continue

            if not _is_tar_key(key):
                raise ValueError(
                    f"Unsupported CLI batch object (expected .tar.gz or "
                    f"scan-manifest.json): {key}"
                )

            entries = _read_snapshot_entries(payload)
            duplicates = seen_paths.intersection(entries)
            if duplicates:
                raise ValueError(f"Duplicate CLI file: {min(duplicates)}")
            seen_paths.update(entries)
            for file in _text_files(entries):
                files[file["path"]] = file

        analyzable = [files[path] for path in sorted(files)]
        focused = _apply_focus(analyzable, scan_manifest)
        if not focused:
            raise ValueError("CLI batch contains no analyzable UTF-8 text files")
        return CliBatchSnapshot(files=focused, scan_manifest=scan_manifest)

    def get_files(self, batch_id: str) -> list[dict[str, str]]:
        """Compatibility wrapper returning only analyzable/focus files."""
        return self.get_batch(batch_id).files

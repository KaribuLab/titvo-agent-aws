"""Validate expert output and normalize only unambiguous transport differences."""

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from code_analysis.domain.entities.expert_result import ExpertIssue, ExpertResult

ISSUE_CONTRACT = """
Each issue requires title, severity (CRITICAL/HIGH/MEDIUM/LOW), category,
path, line (positive JSON integer), and code (nonempty string).
Copy path exactly from a === FILE: ... === header in THIS batch; never copy
example paths from the prompt or report files only present in RAG context.
Line is the original file's 1-based line number, not a string or range.
Use strings for description, summary and recommendation.
Never invent paths or missing evidence.
"""
RESPONSE_CONTRACT = (
    'Return only a JSON object {"issues": [...]} with no prose.\n'
    + ISSUE_CONTRACT
    + 'If no supported finding exists return {"issues": []}.\n'
)


@dataclass
class ParsedResponse(ExpertResult):
    """Keep rejected data in memory for repair, exposing only reasons in diagnostics."""

    rejected: list[dict[str, Any]] = field(default_factory=list)


def decode_response(content: Any) -> dict:
    """Accept SDK text blocks or fenced JSON without guessing malformed JSON."""
    if isinstance(content, list):
        content = "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in content
            if isinstance(block, (str, dict))
        )
    text = str(content).strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Response must be a JSON object")
    return data


def validate_issue(data: Any, files: list[dict[str, str]]) -> ExpertIssue:
    """Normalize safe path/line differences and reject unsupported evidence."""
    required = {"path", "line", "code", "severity", "category", "title"}
    if not isinstance(data, dict):
        raise ValueError("finding must be an object")
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    normalized = dict(data)
    for key in ("path", "code", "severity", "category", "title"):
        if not isinstance(normalized[key], str) or not normalized[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    path = normalized["path"].strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValueError("path must be relative without traversal")
    source = next((file for file in files if file["path"] == path), None)
    if source is None:
        raise ValueError("path is not in this batch")
    normalized["path"] = path
    line = normalized["line"]
    if isinstance(line, str) and line.strip().isascii() and line.strip().isdigit():
        line = int(line.strip())
    if type(line) is not int or line < 1:
        raise ValueError("line must be a positive integer")
    if line > max(1, len(source["content"].splitlines())):
        raise ValueError("line exceeds original file length")
    normalized["line"] = line
    normalized["severity"] = normalized["severity"].strip().upper()
    if normalized["severity"] not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
        raise ValueError("severity must be CRITICAL, HIGH, MEDIUM or LOW")
    for key in ("description", "summary", "recommendation"):
        value = normalized.get(key, "")
        if value is None:
            normalized[key] = ""
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    return ExpertIssue.from_dict(normalized)


def parse_response(
    content: Any, files: list[dict[str, str]], expert: str
) -> ParsedResponse:
    """Preserve valid findings and explain rejections without logging code."""
    result = ParsedResponse(expert_name=expert, files_analyzed=len(files))
    try:
        data = decode_response(content)
        entries = data.get("issues")
        if not isinstance(entries, list):
            raise ValueError("response must contain an issues list")
    except (ValueError, TypeError) as exc:
        result.error = f"Invalid response envelope: {exc}"
        return result
    for index, entry in enumerate(entries):
        try:
            result.issues.append(validate_issue(entry, files))
        except ValueError as exc:
            result.rejected.append(
                {"source_id": index, "reason": str(exc), "finding": entry}
            )
    if result.rejected:
        details = "; ".join(
            f"finding {r['source_id'] + 1}: {r['reason']}" for r in result.rejected
        )
        result.error = f"{len(result.rejected)} invalid findings: {details}"
    return result


def validate_repair(
    data: Any, original: Any, files: list[dict[str, str]]
) -> ExpertIssue:
    """Require repairs to retain identity and supported source evidence."""
    issue = validate_issue(data, files)
    if not isinstance(original, dict):
        raise ValueError("cannot establish identity of a non-object finding")
    original_path = original.get("path")
    if isinstance(original_path, str):
        original_path = original_path.strip().replace("\\", "/")
        while original_path.startswith("./"):
            original_path = original_path[2:]
    if not isinstance(original_path, str):
        original_path = None
    known_paths = {file["path"] for file in files}
    if original_path in known_paths and issue.path != original_path:
        raise ValueError("repair changed a valid source path")
    code = original.get("code")
    if isinstance(code, str) and code.strip() and issue.code != code:
        raise ValueError("repair changed existing code evidence")
    if (
        original_path not in known_paths
        or not isinstance(code, str)
        or not code.strip()
    ):
        source = next(file for file in files if file["path"] == issue.path)
        if issue.code not in source["content"]:
            raise ValueError("repaired evidence is not present in the source file")
    title = original.get("title")
    if isinstance(title, str) and title.strip() and issue.title != title:
        raise ValueError("repair changed the finding identity")
    severity = original.get("severity")
    if isinstance(severity, str) and severity.strip().upper() in {
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
    }:
        issue.severity = severity.strip().upper()
    return issue

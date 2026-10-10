"""Merge Findings Node for LangGraph workflow.

Final node. Consolidates expert findings in two levels and builds the final
envelope:

* **L1** (deterministic): exact-evidence dedupe by ``(path, line, category,
  normalized code)``, keeping the highest severity.
* **L2** (model-led, per file): findings of the same file are consolidated by
  the model in chunks of at most ``_L2_MAX_FINDINGS_PER_CALL``. Every output
  issue must list the ``source_ids`` it represents; a group whose consolidation
  loses, invents or re-rates a finding is rejected and keeps its L1 findings.

The node also reports an ``incomplete`` block when expert batches failed, and
never lets such a scan end as ``COMPLETED``. When the experts reported a
provider outage (``state.provider_error``) the scan ends ``FAILED`` with an
explicit ``error`` and L2 is skipped: calling the model again would only fail.
"""

import json
import logging
import re
from hashlib import sha256
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from code_analysis.domain.entities.expert_result import ExpertIssue, normalize_code
from code_analysis.domain.services.findings_merger import (
    FindingsMerger,
    max_severity,
)
from code_analysis.infra.adapters.langgraph.state import AgentState
from code_analysis.prompts import get_findings_consolidation_prompt

LOGGER = logging.getLogger(__name__)
CONSOLIDATION_TRACE_VERSION = "2026-10-06-two-level-v5"

_L2_MAX_FINDINGS_PER_CALL = 20
_ISSUE_FIELDS = (
    "title",
    "description",
    "severity",
    "category",
    "path",
    "line",
    "summary",
    "code",
    "recommendation",
)


def _issue_sort_key(issue: ExpertIssue) -> tuple:
    meta = issue.metadata or {}
    return (
        str(meta.get("expert", "")),
        int(meta.get("batch_index", 0) or 0),
        issue.path,
        int(issue.line or 0),
        issue.category,
        issue.title,
    )


class MergeFindingsNode:
    """Node for merging expert findings and determining final status."""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def __call__(self, state: AgentState) -> dict[str, Any]:
        try:
            raw_issues = sorted(state.get("issues", []) or [], key=_issue_sort_key)
            scaned_files = state.get("scaned_files", 0)
            expert_errors = state.get("expert_errors", []) or []
            failed_batches = state.get("failed_batches", []) or []
            expert_metadata = state.get("expert_metadata", {}) or {}
            provider_error = state.get("provider_error") or None

            LOGGER.info(
                "Merging %d issues from experts (%d expert errors, %d failed batches)",
                len(raw_issues),
                len(expert_errors),
                len(failed_batches),
            )
            for error in expert_errors:
                LOGGER.warning("Expert error: %s", error)

            l1_issues = FindingsMerger.dedupe(raw_issues)
            final_issues, l2_metrics = self._consolidate_l2(
                l1_issues,
                skip_reason="provider_unavailable" if provider_error else None,
            )
            metrics = {
                "l1_in": len(raw_issues),
                "l1_out": len(l1_issues),
                **l2_metrics,
            }
            LOGGER.info("Consolidation metrics: %s", metrics)

            incomplete = self._build_incomplete(failed_batches, expert_metadata)

            mcp_error = state.get("mcp_error")
            error_message: str | None = None
            if mcp_error or scaned_files == 0:
                status = "FAILED"
                error_message = mcp_error or "No files scanned"
            elif provider_error:
                status = "FAILED"
                error_message = f"Proveedor LLM no disponible: {provider_error}"
            elif any(i.severity in ("CRITICAL", "HIGH") for i in final_issues):
                status = "FAILED"
            elif final_issues or incomplete:
                status = "WARNING"
            else:
                status = "COMPLETED"

            result: dict[str, Any] = {
                "status": status,
                "scaned_files": scaned_files,
                "issues": [issue.to_dict() for issue in final_issues],
            }
            if error_message:
                result["error"] = error_message
            if incomplete:
                result["incomplete"] = incomplete

            LOGGER.info(
                "Final result: status=%s, files=%d, issues=%d, incomplete=%s",
                status,
                scaned_files,
                len(final_issues),
                bool(incomplete),
            )
            # `issues` is a reducer channel: writing it here would append, not
            # replace. The consolidated list lives in final_output only.
            return {
                "status": status,
                "final_output": result,
                "expert_metadata": {"consolidation": metrics},
            }

        except Exception as e:  # noqa: BLE001
            LOGGER.exception("Merge node failed")
            return {
                "status": "FAILED",
                "error": str(e),
                "final_output": {
                    "status": "FAILED",
                    "scaned_files": state.get("scaned_files", 0),
                    "issues": [],
                    "error": str(e),
                },
            }

    # ------------------------------------------------------------------
    # Incomplete scans
    # ------------------------------------------------------------------

    @staticmethod
    def _build_incomplete(
        failed_batches: list[dict[str, Any]],
        expert_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not failed_batches:
            return None
        ordered = sorted(
            failed_batches,
            key=lambda fb: (
                str(fb.get("expert", "")),
                int(fb.get("batch_index", 0) or 0),
            ),
        )
        total_batches = 0
        for value in expert_metadata.values():
            if isinstance(value, dict):
                total_batches += int(value.get("batches", 0) or 0)
                if value.get("batches", 0) == 0 and value.get("failed_batches"):
                    total_batches += int(value.get("failed_batches", 0) or 0)
        total_batches = max(total_batches, len(ordered))
        paths = sorted({str(p) for fb in ordered for p in (fb.get("paths") or [])})
        return {
            "failed_batches": [
                {
                    "expert": fb.get("expert"),
                    "batch_index": fb.get("batch_index"),
                    "paths": sorted(str(p) for p in (fb.get("paths") or [])),
                    "error": str(fb.get("error", "")),
                }
                for fb in ordered
            ],
            "files_not_fully_analyzed": paths,
            "message": (
                f"Análisis incompleto: {len(ordered)} de {total_batches} lotes "
                f"fallaron; {len(paths)} archivos sin analizar completamente"
            ),
        }

    # ------------------------------------------------------------------
    # L2 consolidation
    # ------------------------------------------------------------------

    def _consolidate_l2(
        self,
        issues: list[ExpertIssue],
        skip_reason: str | None = None,
    ) -> tuple[list[ExpertIssue], dict[str, Any]]:
        metrics: dict[str, Any] = {
            "l2_in": len(issues),
            "l2_out": 0,
            "l2_groups": 0,
            "l2_groups_rejected": 0,
        }
        if self._model is None:
            skip_reason = "missing_model"
        if skip_reason:
            metrics["l2_out"] = len(issues)
            metrics["l2_skipped_reason"] = skip_reason
            LOGGER.info(
                "Findings consolidation skipped: trace_version=%s reason=%s "
                "original_count=%d",
                CONSOLIDATION_TRACE_VERSION,
                skip_reason,
                len(issues),
            )
            return list(issues), metrics

        groups: dict[str, list[ExpertIssue]] = {}
        for issue in issues:
            groups.setdefault(issue.path, []).append(issue)

        output: list[ExpertIssue] = []
        for path, group in groups.items():
            if len(group) < 2:
                output.extend(group)
                continue
            for start in range(0, len(group), _L2_MAX_FINDINGS_PER_CALL):
                chunk = group[start : start + _L2_MAX_FINDINGS_PER_CALL]
                if len(chunk) < 2:
                    output.extend(chunk)
                    continue
                metrics["l2_groups"] += 1
                consolidated, accepted = self._consolidate_group(path, chunk)
                if not accepted:
                    metrics["l2_groups_rejected"] += 1
                output.extend(consolidated)

        metrics["l2_out"] = len(output)
        return output, metrics

    def _consolidate_group(
        self,
        path: str,
        group: list[ExpertIssue],
    ) -> tuple[list[ExpertIssue], bool]:
        """Return ``(issues, accepted)``; on any failure the L1 group is kept."""
        findings = self._build_findings_payload(group)
        try:
            consolidated = self._request_consolidated_issues(findings, group)
            return consolidated, True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Findings consolidation rejected for %s; keeping L1 findings: "
                "trace_version=%s error=%s group_size=%d",
                path,
                CONSOLIDATION_TRACE_VERSION,
                exc,
                len(group),
            )
            return list(group), False

    @staticmethod
    def _build_findings_payload(issues: list[ExpertIssue]) -> list[dict[str, Any]]:
        findings = []
        for idx, issue in enumerate(issues):
            finding = {name: getattr(issue, name) for name in _ISSUE_FIELDS}
            finding["id"] = idx
            findings.append(finding)
        return findings

    def _request_consolidated_issues(
        self,
        findings: list[dict[str, Any]],
        group: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        findings_json = json.dumps(findings, ensure_ascii=False, separators=(",", ":"))
        prompt_template = get_findings_consolidation_prompt()
        prompt_hash = self._hash_text(prompt_template)
        prompt = prompt_template.replace("{{ findings_json }}", findings_json)
        LOGGER.info(
            "Findings consolidation request: trace_version=%s prompt_hash=%s "
            "findings_count=%d findings=%s",
            CONSOLIDATION_TRACE_VERSION,
            prompt_hash,
            len(findings),
            self._summarize_findings(findings),
        )
        response = self._model.invoke([HumanMessage(content=prompt)])
        content = getattr(response, "content", response)
        response_shape = self._describe_response_shape(content)
        LOGGER.info(
            "Findings consolidation response received: trace_version=%s "
            "prompt_hash=%s response_shape=%s response_length=%d",
            CONSOLIDATION_TRACE_VERSION,
            prompt_hash,
            response_shape,
            len(str(content)),
        )
        try:
            data = self._parse_json_object(content)
        except Exception as parse_exc:
            content_text = str(content)
            LOGGER.warning(
                "Findings consolidation parse failed; attempting repair: "
                "trace_version=%s prompt_hash=%s error=%s response_shape=%s "
                "response_length=%d response_preview=%s",
                CONSOLIDATION_TRACE_VERSION,
                prompt_hash,
                parse_exc,
                response_shape,
                len(content_text),
                self._safe_response_preview(content),
            )
            try:
                repaired_content = self._repair_json_response(content_text, prompt_hash)
                data = self._parse_json_object(repaired_content)
            except Exception as repair_exc:
                LOGGER.warning(
                    "Findings consolidation repair failed: trace_version=%s "
                    "prompt_hash=%s repair_attempted=true repair_success=false "
                    "error=%s",
                    CONSOLIDATION_TRACE_VERSION,
                    prompt_hash,
                    repair_exc,
                )
                raise
            LOGGER.info(
                "Findings consolidation repair succeeded: trace_version=%s "
                "prompt_hash=%s repair_attempted=true repair_success=true "
                "repaired_response_length=%d",
                CONSOLIDATION_TRACE_VERSION,
                prompt_hash,
                len(repaired_content),
            )
        consolidated = data.get("issues", [])
        if not isinstance(consolidated, list) or not consolidated:
            raise ValueError("Consolidation response issues must be a non-empty list")
        issues = self._validate_group(consolidated, group)
        LOGGER.info(
            "Findings consolidation accepted: trace_version=%s prompt_hash=%s "
            "input_count=%d output_count=%d output_findings=%s",
            CONSOLIDATION_TRACE_VERSION,
            prompt_hash,
            len(findings),
            len(issues),
            self._summarize_issues(issues),
        )
        return issues

    def _validate_group(
        self,
        consolidated: list[Any],
        group: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        """Accountability check: every input represented, nothing invented."""
        expected_ids = set(range(len(group)))
        seen_ids: list[int] = []
        issues: list[ExpertIssue] = []

        for item in consolidated:
            if not isinstance(item, dict):
                raise ValueError("Consolidated issue must be an object")
            missing = set(_ISSUE_FIELDS) - set(item.keys())
            if missing:
                raise ValueError(
                    f"Consolidated issue missing fields: {sorted(missing)}"
                )
            source_ids = item.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise ValueError("Consolidated issue missing source_ids")
            ids: list[int] = []
            for raw in source_ids:
                try:
                    sid = int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid source id: {raw!r}") from exc
                if sid not in expected_ids:
                    raise ValueError(f"Unknown source id: {sid}")
                ids.append(sid)
            seen_ids.extend(ids)
            sources = [group[i] for i in ids]

            severity = str(item["severity"]).upper()
            if severity != max_severity([s.severity for s in sources]):
                raise ValueError(
                    "Consolidated issue severity must be the max of its sources"
                )
            if str(item["path"]) not in {s.path for s in sources}:
                raise ValueError(f"Consolidated issue invented path: {item['path']}")
            try:
                line = int(item["line"])
            except (TypeError, ValueError) as exc:
                raise ValueError("Consolidated issue line must be an integer") from exc
            if line not in {s.line for s in sources}:
                raise ValueError(
                    f"Consolidated issue invented line: {item['path']}:{line}"
                )
            code = normalize_code(str(item["code"]))
            if code not in {normalize_code(s.code) for s in sources}:
                raise ValueError("Consolidated issue invented code evidence")

            merged_from: set[str] = set()
            for source in sources:
                merged_from.update(
                    str(n) for n in source.metadata.get("merged_from", [])
                )
                if source.metadata.get("expert"):
                    merged_from.add(str(source.metadata["expert"]))
            issues.append(
                ExpertIssue(
                    title=str(item["title"]),
                    description=str(item["description"]),
                    severity=severity,
                    category=str(item["category"]),
                    path=str(item["path"]),
                    line=line,
                    summary=str(item["summary"]),
                    code=str(item["code"]),
                    recommendation=str(item["recommendation"]),
                    metadata={
                        "merged_from": sorted(merged_from),
                        "source_count": len(sources),
                    },
                )
            )

        if sorted(seen_ids) != sorted(expected_ids):
            raise ValueError(
                "source_ids must cover every input finding exactly once "
                f"(expected {sorted(expected_ids)}, got {sorted(seen_ids)})"
            )
        return issues

    # ------------------------------------------------------------------
    # Response handling helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_code(code: str) -> str:
        return normalize_code(code)

    @staticmethod
    def _hash_text(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()[:12]

    def _summarize_issues(self, issues: list[ExpertIssue]) -> list[dict[str, Any]]:
        return [
            {
                "id": idx,
                "title": issue.title[:80],
                "severity": issue.severity,
                "path": issue.path,
                "line": issue.line,
                "code_hash": self._hash_text(normalize_code(issue.code)),
            }
            for idx, issue in enumerate(issues)
        ]

    def _summarize_findings(
        self, findings: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": finding.get("id"),
                "title": str(finding.get("title", ""))[:80],
                "severity": finding.get("severity"),
                "path": finding.get("path"),
                "line": finding.get("line"),
                "code_hash": self._hash_text(
                    normalize_code(str(finding.get("code", "")))
                ),
            }
            for finding in findings
        ]

    def _repair_json_response(self, content: str, prompt_hash: str) -> str:
        repair_prompt = (
            "Convierte la siguiente respuesta a JSON estricto válido. "
            "No cambies el contenido semántico. No agregues explicaciones. "
            "No uses Markdown. Usa comillas dobles JSON. "
            "La respuesta debe empezar con { y terminar con }.\n\n"
            f"Respuesta a reparar:\n{content}"
        )
        response = self._model.invoke([HumanMessage(content=repair_prompt)])
        repaired = str(getattr(response, "content", response))
        LOGGER.info(
            "Findings consolidation repair response received: trace_version=%s "
            "prompt_hash=%s repair_attempted=true repaired_response_length=%d",
            CONSOLIDATION_TRACE_VERSION,
            prompt_hash,
            len(repaired),
        )
        return repaired

    def _parse_json_object(self, content: Any) -> dict[str, Any]:
        errors = []
        for candidate in self._json_candidates(content):
            if isinstance(candidate, dict):
                if "issues" in candidate:
                    return candidate
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                continue
            if not isinstance(data, dict):
                raise ValueError("Consolidation response must be a JSON object")
            return data
        if errors:
            raise ValueError(errors[-1])
        raise ValueError("No JSON object found in consolidation response")

    def _json_candidates(self, content: Any) -> list[dict[str, Any] | str]:
        candidates: list[dict[str, Any] | str] = []
        self._collect_json_candidates(content, candidates)

        unique_candidates: list[dict[str, Any] | str] = []
        seen: set[str] = set()
        for candidate in candidates:
            marker = (
                json.dumps(candidate, sort_keys=True, default=str)
                if isinstance(candidate, dict)
                else candidate
            )
            if marker and marker not in seen:
                seen.add(marker)
                unique_candidates.append(candidate)
        return unique_candidates

    def _collect_json_candidates(
        self,
        content: Any,
        candidates: list[dict[str, Any] | str],
    ) -> None:
        if isinstance(content, dict):
            if "issues" in content:
                candidates.append(content)
            for key in ("text", "content"):
                if key in content:
                    self._collect_json_candidates(content[key], candidates)
            return

        if isinstance(content, list):
            for item in content:
                self._collect_json_candidates(item, candidates)
            return

        if not isinstance(content, str):
            return

        stripped = content.strip()
        if not stripped:
            return
        candidates.append(stripped)

        fenced_blocks = re.findall(
            r"```(?:json)?\s*(.*?)```",
            stripped,
            re.DOTALL | re.IGNORECASE,
        )
        candidates.extend(block.strip() for block in fenced_blocks if block.strip())

        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match:
            candidates.append(match.group(0).strip())

    def _describe_response_shape(self, content: Any) -> str:
        if isinstance(content, str):
            return f"str(len={len(content)})"
        if isinstance(content, list):
            item_shapes = [self._describe_response_shape(item) for item in content[:3]]
            suffix = ",..." if len(content) > 3 else ""
            return f"list(len={len(content)},items=[{','.join(item_shapes)}{suffix}])"
        if isinstance(content, dict):
            keys = ",".join(str(key) for key in list(content.keys())[:6])
            suffix = ",..." if len(content) > 6 else ""
            text_shape = ""
            if "text" in content:
                text_shape = f",text={self._describe_response_shape(content['text'])}"
            return f"dict(keys=[{keys}{suffix}]{text_shape})"
        return type(content).__name__

    def _safe_response_preview(self, content: Any, limit: int = 1200) -> str:
        redacted = self._redact_response_content(content)
        try:
            preview = json.dumps(redacted, ensure_ascii=False, default=str)
        except TypeError:
            preview = str(redacted)
        preview = self._redact_code_fields(preview)
        if len(preview) > limit:
            return f"{preview[:limit]}...(truncated,len={len(preview)})"
        return preview

    def _redact_response_content(self, content: Any) -> Any:
        if isinstance(content, dict):
            redacted = {}
            for key, value in content.items():
                if str(key) == "code":
                    code = normalize_code(str(value))
                    redacted[key] = {
                        "redacted": True,
                        "length": len(str(value)),
                        "hash": self._hash_text(code),
                    }
                else:
                    redacted[key] = self._redact_response_content(value)
            return redacted
        if isinstance(content, list):
            return [self._redact_response_content(item) for item in content]
        return content

    def _redact_code_fields(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            prefix = match.group("prefix")
            value = match.group("value")
            code = normalize_code(value)
            return (
                f'{prefix}{{"redacted":true,"length":{len(value)},'
                f'"hash":"{self._hash_text(code)}"}}'
            )

        return re.sub(
            r"(?P<prefix>[\"']code[\"']\s*:\s*)[\"'](?P<value>[^\"']*)[\"']",
            replace,
            text,
        )

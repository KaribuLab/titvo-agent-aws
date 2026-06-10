"""Merge Findings Node for LangGraph workflow.

Final node that asks the consolidation model for final issues and status.
"""

import json
import logging
import re
from hashlib import sha256
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from code_analysis.domain.entities.expert_result import ExpertIssue
from code_analysis.infra.adapters.langgraph.state import AgentState
from code_analysis.prompts import get_findings_consolidation_prompt

LOGGER = logging.getLogger(__name__)
CONSOLIDATION_TRACE_VERSION = "2026-06-09-agent-only-v4"


class MergeFindingsNode:
    """Node for merging expert findings and determining final status."""

    def __init__(self, model: BaseChatModel | None = None) -> None:
        self._model = model

    def __call__(self, state: AgentState) -> dict[str, Any]:
        """Merge findings and return final result.

        Args:
            state: Current workflow state with all issues

        Returns:
            Final state with status and formatted output
        """
        try:
            issues = state.get("issues", [])
            scaned_files = state.get("scaned_files", 0)
            expert_errors = state.get("expert_errors", [])

            LOGGER.info(
                "Merging %d issues from experts (%d expert errors)",
                len(issues),
                len(expert_errors),
            )

            # Log any expert errors
            if expert_errors:
                for error in expert_errors:
                    LOGGER.warning("Expert error: %s", error)

            unique_issues = self._consolidate_findings(issues)

            LOGGER.info("After consolidation: %d unique issues", len(unique_issues))

            # Determine status
            has_critical_or_high = any(
                issue.severity in ("CRITICAL", "HIGH") for issue in unique_issues
            )

            if has_critical_or_high:
                status = "FAILED"
            elif unique_issues:
                status = "WARNING"
            else:
                status = "COMPLETED"

            # Build final output
            result = {
                "status": status,
                "scaned_files": scaned_files,
                "issues": [issue.to_dict() for issue in unique_issues],
            }

            LOGGER.info(
                "Final result: status=%s, files=%d, issues=%d",
                status,
                scaned_files,
                len(unique_issues),
            )

            # Return final state with consolidated issues so both
            # final_output and state.issues survive the StateGraph schema
            return {
                "status": status,
                "final_output": result,
                "issues": unique_issues,
            }

        except Exception as e:
            LOGGER.exception("Merge node failed")
            return {
                "status": "FAILED",
                "scaned_files": state.get("scaned_files", 0),
                "issues": [],
                "error": str(e),
                "final_output": {
                    "status": "FAILED",
                    "scaned_files": state.get("scaned_files", 0),
                    "issues": [],
                },
            }

    def _consolidate_findings(
        self,
        issues: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        """Use the model to produce a final consolidated findings list."""
        if self._model is None or len(issues) < 2:
            LOGGER.info(
                "Findings consolidation skipped: trace_version=%s reason=%s "
                "original_count=%d original_findings=%s",
                CONSOLIDATION_TRACE_VERSION,
                "missing_model" if self._model is None else "not_enough_issues",
                len(issues),
                self._summarize_issues(issues),
            )
            return issues

        findings = self._build_findings_payload(issues)
        if len(findings) < 2:
            LOGGER.info(
                "Findings consolidation skipped: trace_version=%s "
                "reason=not_enough_findings original_count=%d findings=%s",
                CONSOLIDATION_TRACE_VERSION,
                len(issues),
                self._summarize_findings(findings),
            )
            return issues

        try:
            return self._request_consolidated_issues(findings, issues)
        except Exception as exc:
            LOGGER.warning(
                "Findings consolidation failed; using original findings: "
                "trace_version=%s error=%s original_count=%d original_findings=%s",
                CONSOLIDATION_TRACE_VERSION,
                exc,
                len(issues),
                self._summarize_issues(issues),
            )
            return issues

    def _build_findings_payload(
        self,
        issues: list[ExpertIssue],
    ) -> list[dict[str, Any]]:
        """Serialize all expert findings for model-led consolidation."""
        findings = []
        for idx, issue in enumerate(issues):
            finding = issue.to_dict()
            finding["id"] = idx
            findings.append(finding)
        return findings

    def _request_consolidated_issues(
        self,
        findings: list[dict[str, Any]],
        original_issues: list[ExpertIssue],
    ) -> list[ExpertIssue]:
        findings_json = json.dumps(findings, ensure_ascii=False, separators=(",", ":"))
        prompt_template = get_findings_consolidation_prompt()
        prompt_hash = self._hash_text(prompt_template)
        prompt = prompt_template.replace(
            "{{ findings_json }}",
            findings_json,
        )
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
        if not isinstance(consolidated, list):
            raise ValueError("Consolidation response issues must be a list")
        if not consolidated:
            LOGGER.warning(
                "Findings consolidation returned no issues; using original findings: "
                "trace_version=%s prompt_hash=%s original_count=%d",
                CONSOLIDATION_TRACE_VERSION,
                prompt_hash,
                len(original_issues),
            )
            return original_issues
        issues = [self._issue_from_consolidated_dict(issue) for issue in consolidated]
        self._validate_consolidated_evidence(issues, findings)
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

    @staticmethod
    def _issue_from_consolidated_dict(data: dict[str, Any]) -> ExpertIssue:
        if not isinstance(data, dict):
            raise ValueError("Consolidated issue must be an object")
        required_fields = {
            "title",
            "description",
            "severity",
            "category",
            "path",
            "line",
            "summary",
            "code",
            "recommendation",
        }
        missing = required_fields - set(data.keys())
        if missing:
            raise ValueError(f"Consolidated issue missing fields: {sorted(missing)}")
        return ExpertIssue(
            title=str(data["title"]),
            description=str(data["description"]),
            severity=str(data["severity"]),
            category=str(data["category"]),
            path=str(data["path"]),
            line=int(data["line"]),
            summary=str(data["summary"]),
            code=str(data["code"]),
            recommendation=str(data["recommendation"]),
        )

    def _validate_consolidated_evidence(
        self,
        issues: list[ExpertIssue],
        findings: list[dict[str, Any]],
    ) -> None:
        lines_by_path: dict[str, set[int]] = {}
        codes_by_path: dict[str, set[str]] = {}
        for finding in findings:
            path = str(finding.get("path", ""))
            line = int(finding.get("line", 0))
            lines_by_path.setdefault(path, set()).add(line)
            code = self._normalize_code(str(finding.get("code", "")))
            if code:
                codes_by_path.setdefault(path, set()).add(code)

        for issue in issues:
            if issue.path not in lines_by_path:
                raise ValueError(f"Consolidated issue invented path: {issue.path}")
            if issue.line not in lines_by_path[issue.path]:
                raise ValueError(
                    f"Consolidated issue invented line: {issue.path}:{issue.line}"
                )
            code = self._normalize_code(issue.code)
            allowed_codes = codes_by_path.get(issue.path, set())
            if code and code not in allowed_codes:
                raise ValueError("Consolidated issue invented code evidence")

    @staticmethod
    def _normalize_code(code: str) -> str:
        return " ".join((code or "").split())

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
                "code_hash": self._hash_text(self._normalize_code(issue.code)),
            }
            for idx, issue in enumerate(issues)
        ]

    def _summarize_findings(
        self,
        findings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": finding.get("id"),
                "title": str(finding.get("title", ""))[:80],
                "severity": finding.get("severity"),
                "path": finding.get("path"),
                "line": finding.get("line"),
                "code_hash": self._hash_text(
                    self._normalize_code(str(finding.get("code", "")))
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
                    code = self._normalize_code(str(value))
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
            code = self._normalize_code(value)
            return (
                f'{prefix}{{"redacted":true,"length":{len(value)},'
                f'"hash":"{self._hash_text(code)}"}}'
            )

        return re.sub(
            r"(?P<prefix>[\"']code[\"']\s*:\s*)[\"'](?P<value>[^\"']*)[\"']",
            replace,
            text,
        )

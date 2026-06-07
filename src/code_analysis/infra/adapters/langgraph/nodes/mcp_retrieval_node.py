"""MCP Retrieval Node for LangGraph workflow.

This node handles Phase 1 (git.commit-files) and Phase 2 (files) of the MCP workflow.

The MCP gateway exposes ``mcp.tool.git.commit-files`` as an *async* tool: the first call
returns ``jobId`` and ``pollToolName``. Callers MUST poll
``mcp.tool.git.commit-files.poll`` until ``status`` is SUCCESS or FAILURE, then read
``filesPaths``.
"""

import asyncio
import json
import logging
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

GIT_COMMIT_POLL_TOOL = "mcp.tool.git.commit-files.poll"
POLL_INTERVAL_SEC = 1.0
POLL_MAX_ATTEMPTS = 180  # hasta ~3 min (Lambda/SQS en LocalStack pueden ir lentos)


class MCPRetrievalNode:
    """Node for retrieving files via MCP tools.

    Executes:
    1. git.commit-files (async) - poll until complete
    2. files (sync) - for each file path retrieved
    """

    def __init__(self, mcp_client: MultiServerMCPClient):
        self._mcp_client = mcp_client

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        """Execute MCP retrieval phases.

        Args:
            state: Current workflow state with task parameters

        Returns:
            State updates with files or error
        """
        try:
            LOGGER.info(
                "[MCP Node] Starting retrieval for %s @ %s",
                state["repository_url"],
                state["commit_hash"],
            )

            # Phase 1: Call git.commit-files
            LOGGER.debug("[MCP Node] Getting tools from MCP client...")
            tools = await self._mcp_client.get_tools()
            LOGGER.debug("[MCP Node] Got %d tools", len(tools))
            for tool in tools:
                LOGGER.debug("[MCP Node] Available tool: %s", tool.name)
            # Try both naming conventions
            git_commit_files_tool = self._get_tool(tools, "mcp.tool.git.commit-files")
            if git_commit_files_tool is None:
                git_commit_files_tool = self._get_tool(tools, "git.commit-files")

            if git_commit_files_tool is None:
                LOGGER.error(
                    "[MCP Node] git.commit-files tool not found. Available: %s",
                    [t.name for t in tools],
                )
                return {
                    "mcp_error": "git.commit-files tool not available",
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            LOGGER.info(
                "[MCP Node] Found git.commit-files tool, invoking (async job)...",
            )

            # Fase 1a: encolar job — la respuesta es jobId + pollToolName, no rutas
            phase1_raw = await git_commit_files_tool.ainvoke(
                {
                    "repository": state["repository_url"],
                    "commitId": state["commit_hash"],
                }
            )
            LOGGER.debug("[MCP Node] Phase 1 raw type: %s", type(phase1_raw))

            enqueue = self._coerce_dict(phase1_raw)
            job_id = self._extract_job_id(enqueue)
            if not job_id:
                LOGGER.error(
                    "[MCP Node] git.commit-files no devolvió jobId; payload: %s",
                    str(phase1_raw)[:500],
                )
                return {
                    "mcp_error": (
                        "git.commit-files no devolvió jobId (tool asíncrona esperada)"
                    ),
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            poll_tool = self._get_tool(tools, GIT_COMMIT_POLL_TOOL)
            if poll_tool is None:
                poll_tool = self._get_tool(tools, "git.commit-files.poll")

            if poll_tool is None:
                LOGGER.error(
                    "[MCP Node] Falta tool de polling. Disponibles: %s",
                    [t.name for t in tools],
                )
                return {
                    "mcp_error": (
                        f"Tool de polling no encontrado ({GIT_COMMIT_POLL_TOOL})"
                    ),
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            LOGGER.info("[MCP Node] Polling commit-files job jobId=%s ...", job_id)
            polled = await self._poll_git_commit_job(poll_tool, job_id)
            if polled.get("failure"):
                return {
                    "mcp_error": polled["failure"],
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            file_paths = self._extract_file_paths(polled.get("payload"))
            LOGGER.info(
                "[MCP Node] Retrieved %d file paths: %s",
                len(file_paths),
                file_paths[:5],
            )

            if not file_paths:
                LOGGER.error("[MCP Node] No files retrieved from git.commit-files")
                return {
                    "mcp_error": "No files in commit",
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            # Phase 2: Read each file
            files_tool = self._get_tool(tools, "mcp.tool.files")
            if files_tool is None:
                files_tool = self._get_tool(tools, "files")
            if files_tool is None:
                LOGGER.error(
                    "[MCP Node] files tool not found. Available: %s",
                    [t.name for t in tools],
                )
                return {
                    "mcp_error": "files tool not available",
                    "status": "FAILED",
                    "scaned_files": 0,
                    "files": [],
                }

            files_content = []
            for file_path in file_paths:
                try:
                    # Note: files tool only expects 'path' parameter
                    file_result = await files_tool.ainvoke(
                        {
                            "path": file_path,
                        }
                    )

                    content = self._extract_file_content(file_result)
                    if content is not None:
                        files_content.append(
                            {
                                "path": file_path,
                                "content": content,
                            }
                        )
                except Exception as e:
                    LOGGER.warning("Failed to read file %s: %s", file_path, e)
                    # Continue with other files

            LOGGER.info("[MCP Node] Successfully read %d files", len(files_content))

            return {
                "files": files_content,
                "scaned_files": len(files_content),
                "mcp_error": None,
            }

        except Exception as e:
            LOGGER.exception("[MCP Node] MCP retrieval failed with exception")
            return {
                "mcp_error": str(e),
                "status": "FAILED",
                "scaned_files": 0,
                "files": [],
            }

    def _get_tool(
        self,
        tools: list[Any],
        tool_name: str,
    ) -> Any:
        """Find tool by sanitized name."""
        sanitized_name = self._sanitize_tool_name(tool_name)

        for tool in tools:
            if tool.name == tool_name or tool.name == sanitized_name:
                return tool
        return None

    def _sanitize_tool_name(self, name: str) -> str:
        """Sanitize tool name for OpenAI compatibility."""
        import re

        sanitized = re.sub(r"[^a-zA-Z0-9]", "_", name)
        sanitized = re.sub(r"_+", "_", sanitized)
        return sanitized.strip("_")

    def _extract_file_paths(self, result: Any) -> list[str]:
        """Extract file paths from Phase 1 result."""
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                if isinstance(data.get("filesPaths"), list):
                    paths = data["filesPaths"]
                    return [p for p in paths if isinstance(p, str)]

            if isinstance(result.get("filesPaths"), list):
                paths = result["filesPaths"]
                return [p for p in paths if isinstance(p, str)]

            for key in ("files", "file_paths"):
                raw = result.get(key)
                if isinstance(raw, list):
                    return [p for p in raw if isinstance(p, str)]

            content = result.get("content")
            if isinstance(content, list):
                return [p for p in content if isinstance(p, str)]

            return []

        if isinstance(result, list):
            return [p for p in result if isinstance(p, str)]

        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                return []
            return self._extract_file_paths(parsed)

        return []

    async def _poll_git_commit_job(
        self,
        poll_tool: Any,
        job_id: str,
    ) -> dict[str, Any]:
        """Esperar al job y devolver payload de éxito o mensaje de fallo."""

        for attempt in range(POLL_MAX_ATTEMPTS):
            raw = await poll_tool.ainvoke({"jobId": job_id})
            payload = self._coerce_dict(raw)
            if payload is None:
                LOGGER.warning(
                    "[MCP Node] Poll intento %d: resultado no parseable: %s",
                    attempt + 1,
                    str(raw)[:300],
                )
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            status = str(payload.get("status", "") or "").upper()
            LOGGER.debug("[MCP Node] Poll status=%s (intento %d)", status, attempt + 1)

            if status in ("SUCCESS",):
                return {"payload": payload}
            if status in ("FAILURE",):
                msg = payload.get("message") or payload.get("error") or "Job FAILURE"
                return {"failure": f"git-commit-files job falló: {msg}"}
            # REQUESTED, IN_PROGRESS, vacío...
            await asyncio.sleep(POLL_INTERVAL_SEC)

        return {
            "failure": (
                f"Timeout esperando git-commit-files (jobId={job_id}, "
                f"{POLL_MAX_ATTEMPTS} intentos)"
            ),
        }

    def _coerce_dict(self, result: Any) -> dict[str, Any] | None:
        """Normaliza respuestas MCP (dict, JSON string o objetos con .content)."""
        if result is None:
            return None
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        content = getattr(result, "content", None)
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _extract_job_id(self, data: dict[str, Any] | None) -> str | None:
        if not data:
            return None
        jid = data.get("jobId") or data.get("job_id")
        return jid if isinstance(jid, str) and jid.strip() else None

    def _extract_file_content(self, result: Any) -> str | None:
        """Extract content from file read result."""
        if isinstance(result, dict):
            if "content" in result:
                return result["content"]
            if "file_content" in result:
                return result["file_content"]
        elif isinstance(result, str):
            return result
        return None

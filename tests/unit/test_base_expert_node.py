"""Tests for BaseExpertNode: runtime selection, chunking/batching, concurrency,
retries, failed batches and line mapping.

A given file's content sent to the LLM must not depend on how many other
files are being scanned alongside it, and no file is ever truncated.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_analysis.domain.services.batch_planner import plan_files
from code_analysis.infra.adapters.langgraph.nodes import base_expert_node as mod
from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    ExpertRuntimeConfig,
    is_suspicion_title,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
    OwaspApiNode,
    OwaspMobileNode,
)


def _file(path: str, content: str, runtimes=("server",)) -> dict:
    return {"path": path, "content": content, "runtimes": list(runtimes)}


def _lines(n: int, width: int = 99) -> str:
    return "".join(f"x{i:06d}".ljust(width, "-") + "\n" for i in range(n))


def _issue_json(path: str, line: int, title: str = "Issue") -> dict:
    return {
        "title": title,
        "description": "d",
        "severity": "HIGH",
        "category": "Cat",
        "path": path,
        "line": line,
        "summary": "s",
        "code": "code();",
        "recommendation": "r",
    }


def _response(issues: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(content=json.dumps({"issues": issues}))


def _extract_file_block(formatted: str, path: str) -> str:
    marker = f"=== FILE: {path} "
    start = formatted.index(marker)
    end = formatted.index("=== END FILE ===", start)
    return formatted[start:end]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _instant)


class TestContentIndependentOfScanSize:
    def test_same_file_same_content_in_small_and_large_scan(self):
        node = CodeVulnerabilitiesNode(None)
        target = _file("src/big_file.py", _lines(120))

        small = node._format_files([target, _file("o.py", "x")])
        large = node._format_files(
            [target] + [_file(f"o{i}.py", "x" * 5_000) for i in range(400)]
        )

        assert _extract_file_block(small, target["path"]) == _extract_file_block(
            large, target["path"]
        )

    def test_no_file_is_truncated(self):
        node = CodeVulnerabilitiesNode(None)
        files = [_file(f"f{i}.py", "y" * 25_000) for i in range(20)]  # 500k chars
        formatted = node._format_files(files)
        for f in files:
            assert f["content"] in formatted

    def test_header_carries_runtime(self):
        node = CodeVulnerabilitiesNode(None)
        formatted = node._format_files([_file("src/a.tsx", "x", ("browser",))])
        assert "=== FILE: src/a.tsx [runtime: browser] ===" in formatted

    def test_partial_chunk_header_carries_line_range(self):
        node = CodeVulnerabilitiesNode(None)
        formatted = node._format_files([_file("big.py", "import os\n" + _lines(800))])
        assert "[lines 1-" in formatted
        assert "Report `line` as the absolute line number" in formatted


class TestRuntimeSelection:
    def test_selects_by_intersection(self):
        node = OwaspMobileNode(None)
        files = [
            _file("a.tsx", "x", ("mobile", "browser")),
            _file("b.tsx", "x", ("browser",)),
            _file("c.kt", "x", ("mobile",)),
        ]
        assert [f["path"] for f in node._select_files(files)] == ["a.tsx", "c.kt"]

    def test_missing_runtimes_is_treated_as_unknown(self):
        assert OwaspApiNode(None).matches_runtimes(None) is True
        assert OwaspMobileNode(None).matches_runtimes(None) is False

    @pytest.mark.asyncio
    async def test_no_matching_files_skips_without_llm_call(self):
        model = MagicMock()
        model.ainvoke = AsyncMock()
        node = OwaspMobileNode(model)
        result = await node({"files": [_file("a.py", "x", ("server",))], "issues": []})
        model.ainvoke.assert_not_called()
        assert result["expert_metadata"]["owasp_mobile"] == {
            "files_analyzed": 0,
            "skipped": True,
        }
        assert "issues" not in result


class TestBatchesAndConcurrency:
    @pytest.mark.asyncio
    async def test_one_llm_call_per_batch_and_results_in_batch_order(self):
        files = [
            _file(f"f{i:02d}.py", "x" * 20_000) for i in range(30)
        ]  # 600k → 3 batches
        config = ExpertRuntimeConfig(max_concurrency=8)
        expected_batches = plan_files(files, 30_000, 200_000, 5_000)
        assert len(expected_batches) == 3

        calls: list[str] = []

        async def _ainvoke(messages):
            content = messages[1].content
            first_path = content.split("=== FILE: ")[1].split(" ")[0]
            calls.append(first_path)
            # Later batches answer faster to shuffle completion order.
            await asyncio.sleep(0)
            return _response([_issue_json(first_path, 1, title=f"from {first_path}")])

        model = MagicMock()
        model.ainvoke = _ainvoke
        node = CodeVulnerabilitiesNode(model, config)

        result = await node({"files": files, "issues": []})

        assert len(calls) == 3
        titles = [i.title for i in result["issues"]]
        assert titles == [f"from {b.chunks[0].path}" for b in expected_batches]
        assert [i.metadata["batch_index"] for i in result["issues"]] == [0, 1, 2]
        assert result["expert_metadata"]["code_vulnerabilities"]["batches"] == 3
        assert result["failed_batches"] == []

    @pytest.mark.asyncio
    async def test_semaphore_bounds_concurrency(self):
        files = [_file(f"f{i:02d}.py", "x" * 20_000) for i in range(60)]  # 6 batches
        config = ExpertRuntimeConfig(max_concurrency=2)
        active = 0
        peak = 0

        async def _ainvoke(messages):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return _response([])

        model = MagicMock()
        model.ainvoke = _ainvoke
        await CodeVulnerabilitiesNode(model, config)({"files": files, "issues": []})
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_issue_metadata_records_origin(self):
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=_response([_issue_json("a.py", 3)]))
        node = OwaspApiNode(model)
        result = await node({"files": [_file("a.py", "x")], "issues": []})
        assert result["issues"][0].metadata == {
            "expert": "owasp_api",
            "batch_index": 0,
            "chunk_index": 0,
        }


class TestFailedBatches:
    @pytest.mark.asyncio
    async def test_failed_batch_recorded_and_others_kept(self):
        files = [_file(f"f{i:02d}.py", "x" * 20_000) for i in range(30)]  # 3 batches
        config = ExpertRuntimeConfig(max_attempts=3)

        async def _ainvoke(messages):
            content = messages[1].content
            first_path = content.split("=== FILE: ")[1].split(" ")[0]
            if first_path == "f10.py":  # second batch
                raise RuntimeError("provider down")
            return _response([_issue_json(first_path, 1)])

        model = MagicMock()
        model.ainvoke = _ainvoke
        result = await CodeVulnerabilitiesNode(model, config)(
            {"files": files, "issues": []}
        )

        assert len(result["issues"]) == 2
        assert len(result["failed_batches"]) == 1
        failed = result["failed_batches"][0]
        assert failed["expert"] == "code_vulnerabilities"
        assert failed["batch_index"] == 1
        assert failed["paths"] == [f"f{i:02d}.py" for i in range(10, 20)]
        assert "provider down" in failed["error"]
        assert result["expert_errors"] == [
            "code_vulnerabilities: batch 1 failed: provider down"
        ]
        assert result["expert_metadata"]["code_vulnerabilities"]["failed_batches"] == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        model = MagicMock()
        model.ainvoke = AsyncMock(
            side_effect=[RuntimeError("429"), RuntimeError("429"), _response([])]
        )
        node = OwaspApiNode(model, ExpertRuntimeConfig(max_attempts=3))
        result = await node({"files": [_file("a.py", "x")], "issues": []})
        assert model.ainvoke.await_count == 3
        assert result["failed_batches"] == []

    @pytest.mark.asyncio
    async def test_exhausted_retries_fail_batch(self):
        model = MagicMock()
        model.ainvoke = AsyncMock(side_effect=RuntimeError("429"))
        node = OwaspApiNode(model, ExpertRuntimeConfig(max_attempts=3))
        result = await node({"files": [_file("a.py", "x")], "issues": []})
        assert model.ainvoke.await_count == 3
        assert len(result["failed_batches"]) == 1

    @pytest.mark.asyncio
    async def test_unparsable_response_is_a_failed_batch(self):
        model = MagicMock()
        model.ainvoke = AsyncMock(return_value=SimpleNamespace(content="not json"))
        node = OwaspApiNode(model)
        result = await node({"files": [_file("a.py", "x")], "issues": []})
        assert result["issues"] == []
        assert result["failed_batches"][0]["error"] == "Failed to parse JSON response"


class TestLineMapping:
    def test_single_chunk_line_untouched(self):
        node = CodeVulnerabilitiesNode(None)
        batch = plan_files([_file("a.py", "x\n" * 10)])[0]
        result = node._parse_response(
            json.dumps({"issues": [_issue_json("a.py", 7)]}), batch
        )
        assert result.issues[0].line == 7

    def test_relative_line_is_offset_to_absolute(self):
        node = CodeVulnerabilitiesNode(None)
        content = "import os\n" + _lines(800)
        batches = plan_files([_file("a.py", content)], 30_000, 10**9, 5_000)
        chunk = batches[0].chunks[1]
        assert chunk.start_line > 1
        result = node._parse_response(
            json.dumps({"issues": [_issue_json("a.py", 40)]}), batches[0]
        )
        # Line 40 is not inside chunk 0's absolute range? It is (1-300), so chunk 0
        # is chosen and 40 stays absolute. Use a line only chunk 1 can hold.
        assert result.issues[0].line == 40

        relative = json.dumps({"issues": [_issue_json("a.py", chunk.start_line + 5)]})
        result = node._parse_response(relative, batches[0])
        assert result.issues[0].line == chunk.start_line + 5

    def test_resolve_line_offsets_when_only_relative_fits(self):
        content = "import os\n" + _lines(800)
        batches = plan_files([_file("a.py", content)], 30_000, 10**9, 5_000)
        chunk = batches[0].chunks[2]
        # A value below the chunk's absolute range but within its relative span.
        mapped = CodeVulnerabilitiesNode._resolve_line(40, chunk)
        assert mapped == chunk.start_line - 1 + 40

    def test_resolve_line_keeps_absolute_in_range(self):
        content = "import os\n" + _lines(800)
        batches = plan_files([_file("a.py", content)], 30_000, 10**9, 5_000)
        chunk = batches[0].chunks[2]
        assert CodeVulnerabilitiesNode._resolve_line(chunk.start_line + 3, chunk) == (
            chunk.start_line + 3
        )


class TestExpertRuntimeConfig:
    def test_defaults(self):
        config = ExpertRuntimeConfig()
        assert config.to_dict() == {
            "per_file_cap_chars": 30_000,
            "batch_budget_chars": 200_000,
            "chunk_overlap_chars": 5_000,
            "max_concurrency": 4,
        }

    def test_from_env_with_fallbacks(self, monkeypatch):
        monkeypatch.delenv(mod.ENV_FILE_CAP, raising=False)
        monkeypatch.setenv(mod.ENV_BATCH_BUDGET, "150000")
        monkeypatch.setenv(mod.ENV_CHUNK_OVERLAP, "not-a-number")
        monkeypatch.setenv(mod.ENV_MAX_CONCURRENCY, "0")
        config = ExpertRuntimeConfig.from_env()
        assert config.per_file_cap_chars == 30_000
        assert config.batch_budget_chars == 150_000
        assert config.chunk_overlap_chars == 5_000
        assert config.max_concurrency == 4


class TestSuspicionSeverityCap:
    def _parse(self, title: str, severity: str):
        node = OwaspApiNode(None)
        batch = plan_files([_file("a.ts", "x\n", ("browser",))])[0]
        issue = {**_issue_json("a.ts", 1, title=title), "severity": severity}
        return node._parse_response(json.dumps({"issues": [issue]}), batch).issues[0]

    @pytest.mark.parametrize(
        "title",
        [
            "Sospecha: token expuesto",
            "sospecha: token expuesto",
            "SOSPECHA: token expuesto",
            "  Sospecha:   token",
            "Sospécha: token",  # accent-insensitive
        ],
    )
    def test_high_suspicion_is_capped_to_medium(self, title):
        issue = self._parse(title, "HIGH")
        assert issue.severity == "MEDIUM"
        assert issue.metadata["severity_capped"] is True

    def test_critical_suspicion_is_capped_to_medium(self):
        issue = self._parse("Sospecha: clave", "CRITICAL")
        assert issue.severity == "MEDIUM"
        assert issue.metadata["severity_capped"] is True

    def test_low_suspicion_is_untouched(self):
        issue = self._parse("Sospecha: menor", "LOW")
        assert issue.severity == "LOW"
        assert "severity_capped" not in issue.metadata

    def test_confirmed_high_is_untouched(self):
        issue = self._parse("Token expuesto", "HIGH")
        assert issue.severity == "HIGH"
        assert "severity_capped" not in issue.metadata

    def test_prefix_inside_title_does_not_count(self):
        issue = self._parse("Token con Sospecha: algo", "HIGH")
        assert issue.severity == "HIGH"

    def test_is_suspicion_title_helper(self):
        assert is_suspicion_title("Sospecha: x")
        assert not is_suspicion_title("Sospechoso: x")
        assert not is_suspicion_title(None)


class TestComposedSystemMessage:
    @pytest.mark.asyncio
    async def test_expert_system_message_starts_with_preamble(self):
        from code_analysis import prompts

        captured = {}

        async def _ainvoke(messages):
            captured["system"] = messages[0].content
            return _response([])

        model = MagicMock()
        model.ainvoke = _ainvoke
        await OwaspApiNode(model)({"files": [_file("a.py", "x")], "issues": []})

        assert captured["system"].startswith(prompts.get_common_preamble().rstrip())
        assert "OWASP API Security Expert" in captured["system"]

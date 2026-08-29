"""Tests for BaseExpertNode's per-file content budget (truncation).

A given file's truncated content must not depend on how many other files
are being scanned alongside it in the same commit — otherwise the same
unchanged file can be analyzed with different detail across commits,
producing findings that appear/disappear without the file itself changing.
"""

from code_analysis.infra.adapters.langgraph.nodes.base_expert_node import (
    _GLOBAL_BUDGET_CHARS,
    _compute_file_budgets,
)
from code_analysis.infra.adapters.langgraph.nodes.expert_nodes import (
    CodeVulnerabilitiesNode,
)


def _make_file(path: str, size: int) -> dict:
    return {"path": path, "content": "x" * size}


def _extract_file_block(formatted: str, path: str) -> str:
    marker = f"=== FILE: {path} ==="
    start = formatted.index(marker)
    end = formatted.index("=== END FILE ===", start)
    return formatted[start:end]


class TestFileBudgetIndependentOfCommitSize:
    def test_same_large_file_same_truncation_in_small_and_large_commit(self):
        node = CodeVulnerabilitiesNode(None)
        target = _make_file("src/big_file.py", 50_000)

        small_commit = [target] + [
            _make_file(f"other_{i}.py", 100) for i in range(2)
        ]
        large_commit = [target] + [
            _make_file(f"other_{i}.py", 100) for i in range(49)
        ]

        small_output = node._format_files(small_commit)
        large_output = node._format_files(large_commit)

        small_block = _extract_file_block(small_output, target["path"])
        large_block = _extract_file_block(large_output, target["path"])

        assert small_block == large_block

    def test_untouched_small_file_never_truncated_regardless_of_commit_size(self):
        node = CodeVulnerabilitiesNode(None)
        target = _make_file("src/small_file.py", 500)

        small_commit = [target]
        large_commit = [target] + [
            _make_file(f"other_{i}.py", 100) for i in range(49)
        ]

        small_output = node._format_files(small_commit)
        large_output = node._format_files(large_commit)

        assert target["content"] in small_output
        assert target["content"] in large_output


class TestAggregateBudgetSafetyNet:
    """The per-commit total must still fit the model's context window — but
    the shrink, when needed, must be driven by actual aggregate content size,
    not by how many files happen to be in the commit."""

    def test_huge_aggregate_content_still_gets_truncated(self):
        node = CodeVulnerabilitiesNode(None)
        # 10 files x 40k chars = 400k chars, well above the global budget.
        huge_commit = [_make_file(f"f_{i}.py", 40_000) for i in range(10)]

        output = node._format_files(huge_commit)

        assert len(output) < sum(len(f["content"]) for f in huge_commit)

    def test_floor_never_pushes_aggregate_past_global_budget(self):
        """Many small/medium files can each land below the per-file floor
        after proportional shrink; honoring the floor for all of them would
        blow past the global budget, so the floor must yield to the
        aggregate ceiling instead."""
        # 100 files x 4k chars = 400k raw, well above the 200k global budget.
        # Proportional shrink alone would give each file 2k chars (below the
        # 3k floor); honoring the floor for all 100 would total 300k > 200k.
        many_files = [_make_file(f"f_{i}.py", 4_000) for i in range(100)]

        budgets = _compute_file_budgets(many_files)

        assert sum(budgets.values()) <= _GLOBAL_BUDGET_CHARS

    def test_shrink_driven_by_total_size_not_file_count(self):
        node = CodeVulnerabilitiesNode(None)
        # Keep every file at or below the per-file cap (30k) so the totals
        # below are reached without any individual file being capped first.
        target = _make_file("src/target.py", 25_000)

        # Same total content (250k chars) reached via few big files vs many
        # medium files — the target file's budget should be the same either way.
        few_big_files = [target] + [_make_file(f"f_{i}.py", 25_000) for i in range(9)]
        many_medium_files = [target] + [
            _make_file(f"f_{i}.py", 15_000) for i in range(15)
        ]

        few_output = node._format_files(few_big_files)
        many_output = node._format_files(many_medium_files)

        few_block = _extract_file_block(few_output, target["path"])
        many_block = _extract_file_block(many_output, target["path"])

        assert few_block == many_block

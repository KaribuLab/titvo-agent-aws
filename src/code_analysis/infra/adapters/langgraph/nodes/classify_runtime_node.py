"""Classify Runtime Node for LangGraph workflow.

Runs after retrieval and before the expert fan-out. Builds the deterministic
project profile, tags every file with its ordered runtime list and records
coverage metadata so a file silently reaching no domain expert is impossible.
"""

import logging
from typing import Any

from code_analysis.domain.services.runtime_classifier import (
    Runtime,
    build_project_profile,
    classify_values,
)
from code_analysis.infra.adapters.langgraph.state import AgentState

LOGGER = logging.getLogger(__name__)

_ALL_RUNTIME_VALUES = frozenset(r.value for r in Runtime)


class ClassifyRuntimeNode:
    """Annotate ``state.files`` with ``runtimes`` and expose coverage metadata.

    Args:
        expert_runtimes: mapping ``expert_name -> set of runtime values`` the
            expert selects. Experts selecting every runtime are not considered
            "domain" experts for the coverage invariant.
    """

    def __init__(self, expert_runtimes: dict[str, set[str]]):
        self._expert_runtimes = {k: set(v) for k, v in expert_runtimes.items()}
        self._domain_experts = {
            name: runtimes
            for name, runtimes in self._expert_runtimes.items()
            if not runtimes >= _ALL_RUNTIME_VALUES
        }

    def __call__(self, state: AgentState) -> dict[str, Any]:
        files = state.get("files", []) or []
        profile = build_project_profile(files)

        classified: list[dict[str, Any]] = []
        coverage: dict[str, int] = {name: 0 for name in self._expert_runtimes}
        without_domain_expert = 0

        for file in files:
            path = file.get("path", "")
            runtimes = classify_values(path, file.get("content", "") or "", profile)
            if not self._has_domain_expert(runtimes):
                without_domain_expert += 1
                LOGGER.warning(
                    "[Classify Node] %s (%s) matched no domain expert; "
                    "adding 'unknown'",
                    path,
                    runtimes,
                )
                runtimes = [*runtimes, Runtime.UNKNOWN.value]
            runtime_set = set(runtimes)
            for name, expert_runtimes in self._expert_runtimes.items():
                if runtime_set & expert_runtimes:
                    coverage[name] += 1
            classified.append({**file, "runtimes": runtimes})

        LOGGER.info(
            "[Classify Node] %d files classified; profile=%s coverage=%s",
            len(classified),
            profile.to_dict(),
            coverage,
        )
        return {
            "files": classified,
            "expert_metadata": {
                "project_profile": profile.to_dict(),
                "coverage": {
                    **coverage,
                    "files_without_domain_expert": without_domain_expert,
                },
            },
        }

    def _has_domain_expert(self, runtimes: list[str]) -> bool:
        runtime_set = set(runtimes)
        return any(runtime_set & rts for rts in self._domain_experts.values())

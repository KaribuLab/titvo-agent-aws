"""Chunking and batching of analysed files for expert LLM calls.

Files are never truncated. A file larger than ``per_file_cap`` is split into
overlapping windows cut at line boundaries; every window after the first is
prefixed with the structural lines (imports, signatures) of the whole file so
the model keeps the surrounding context. Chunks are then packed greedily into
batches bounded by ``batch_budget``. Both steps are deterministic: the same
input always yields the same chunks and batches.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_analysis.domain.services.structural_lines import (
    extract_structural_lines,
)

DEFAULT_PER_FILE_CAP_CHARS = 30_000
DEFAULT_BATCH_BUDGET_CHARS = 200_000
DEFAULT_CHUNK_OVERLAP_CHARS = 5_000
DEFAULT_PREFIX_MAX_LINES = 60

_PREFIX_HEADER = (
    "# [context: imports and signatures of {path} — not part of this chunk]"
)
_PREFIX_FOOTER = "# [end context]"


@dataclass(frozen=True)
class Chunk:
    """A window of a file ready to be sent to an expert."""

    path: str
    runtimes: tuple[str, ...]
    body: str
    prefix: str
    start_line: int
    end_line: int
    total_lines: int
    chunk_index: int
    total_chunks: int

    @property
    def size(self) -> int:
        return len(self.prefix) + len(self.body)

    @property
    def is_partial(self) -> bool:
        return self.total_chunks > 1

    @property
    def primary_runtime(self) -> str:
        return self.runtimes[0] if self.runtimes else "unknown"


@dataclass(frozen=True)
class Batch:
    index: int
    chunks: tuple[Chunk, ...]

    @property
    def size(self) -> int:
        return sum(chunk.size for chunk in self.chunks)

    @property
    def paths(self) -> list[str]:
        return sorted({chunk.path for chunk in self.chunks})


def split_file(
    file: dict,
    per_file_cap: int = DEFAULT_PER_FILE_CAP_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
    prefix_max_lines: int = DEFAULT_PREFIX_MAX_LINES,
) -> list[Chunk]:
    """Split one file into chunks covering every line of its content."""
    path = file["path"]
    content = file.get("content", "") or ""
    runtimes = tuple(file.get("runtimes") or ("unknown",))
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    if (
        len(content) <= per_file_cap
        or total_lines <= 1
        and len(content) <= per_file_cap
    ):
        return [
            Chunk(
                path=path,
                runtimes=runtimes,
                body=content,
                prefix="",
                start_line=1,
                end_line=max(total_lines, 1),
                total_lines=max(total_lines, 1),
                chunk_index=0,
                total_chunks=1,
            )
        ]

    structural = extract_structural_lines(content, max_lines=prefix_max_lines)
    prefix = ""
    if structural:
        prefix = (
            _PREFIX_HEADER.format(path=path)
            + "\n"
            + "\n".join(structural)
            + "\n"
            + _PREFIX_FOOTER
            + "\n"
        )
    body_cap_after_first = max(per_file_cap - len(prefix), per_file_cap // 2)

    windows = _line_windows(lines, per_file_cap, body_cap_after_first, overlap_chars)

    chunks: list[Chunk] = []
    total_chunks = len(windows)
    for index, (start_idx, end_idx, body) in enumerate(windows):
        chunks.append(
            Chunk(
                path=path,
                runtimes=runtimes,
                body=body,
                prefix=prefix if index > 0 else "",
                start_line=start_idx + 1,
                end_line=end_idx,
                total_lines=total_lines,
                chunk_index=index,
                total_chunks=total_chunks,
            )
        )
    return chunks


def plan(
    chunks: list[Chunk],
    batch_budget: int = DEFAULT_BATCH_BUDGET_CHARS,
) -> list[Batch]:
    """Pack chunks into bounded batches, preserving a deterministic order."""
    ordered = sorted(chunks, key=lambda c: (c.primary_runtime, c.path, c.chunk_index))
    batches: list[Batch] = []
    current: list[Chunk] = []
    current_size = 0
    for chunk in ordered:
        if current and current_size + chunk.size > batch_budget:
            batches.append(Batch(index=len(batches), chunks=tuple(current)))
            current = []
            current_size = 0
        current.append(chunk)
        current_size += chunk.size
    if current:
        batches.append(Batch(index=len(batches), chunks=tuple(current)))
    return batches


def plan_files(
    files: list[dict],
    per_file_cap: int = DEFAULT_PER_FILE_CAP_CHARS,
    batch_budget: int = DEFAULT_BATCH_BUDGET_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP_CHARS,
) -> list[Batch]:
    """Convenience: split every file and pack the resulting chunks."""
    chunks: list[Chunk] = []
    for file in files:
        chunks.extend(split_file(file, per_file_cap, overlap_chars))
    return plan(chunks, batch_budget)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line_windows(
    lines: list[str],
    first_cap: int,
    later_cap: int,
    overlap_chars: int,
) -> list[tuple[int, int, str]]:
    """Return ``(start_idx, end_idx_exclusive, body)`` windows over *lines*.

    Windows are cut at line boundaries. A single line longer than the cap is
    emitted on its own (it cannot be split without breaking line numbering).
    Consecutive windows overlap by roughly *overlap_chars* characters.
    """
    windows: list[tuple[int, int, str]] = []
    start = 0
    total = len(lines)
    cap = first_cap
    while start < total:
        end = start
        size = 0
        while end < total:
            line_len = len(lines[end])
            if size + line_len > cap and end > start:
                break
            size += line_len
            end += 1
        windows.append((start, end, "".join(lines[start:end])))
        if end >= total:
            break
        # Walk back from `end` to build the overlap, but always make progress.
        next_start = end
        overlap = 0
        while next_start - 1 > start and overlap < overlap_chars:
            next_start -= 1
            overlap += len(lines[next_start])
        if next_start <= start:
            next_start = start + 1
        start = next_start
        cap = later_cap
    return windows

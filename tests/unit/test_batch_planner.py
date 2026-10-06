"""Tests for chunking and batching of files."""

from code_analysis.domain.services.batch_planner import (
    Batch,
    plan,
    plan_files,
    split_file,
)


def _file(path: str, content: str, runtimes=("server",)) -> dict:
    return {"path": path, "content": content, "runtimes": list(runtimes)}


def _lines(n: int, width: int = 99) -> str:
    # Each line is `width` chars + newline -> deterministic sizes.
    return "".join(f"x{i:06d}".ljust(width, "-") + "\n" for i in range(n))


class TestSplitFile:
    def test_small_file_is_single_chunk(self):
        chunks = split_file(_file("a.py", "import os\nprint(1)\n"), per_file_cap=30_000)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.body == "import os\nprint(1)\n"
        assert chunk.prefix == ""
        assert (chunk.start_line, chunk.end_line, chunk.total_lines) == (1, 2, 2)
        assert chunk.is_partial is False

    def test_file_at_cap_is_not_split(self):
        content = _lines(300)  # 300 * 100 = 30_000 chars
        chunks = split_file(_file("a.py", content), per_file_cap=30_000)
        assert len(chunks) == 1

    def test_large_file_is_fully_covered_with_overlap(self):
        content = "import os\n" + _lines(780)  # ≈ 78k chars, 781 lines
        chunks = split_file(
            _file("a.py", content), per_file_cap=30_000, overlap_chars=5_000
        )

        assert len(chunks) == 3
        covered = set()
        for chunk in chunks:
            covered.update(range(chunk.start_line, chunk.end_line + 1))
        assert covered == set(range(1, 782))

        # Consecutive chunks overlap by about 5k chars (50 lines of 100 chars).
        for prev, nxt in zip(chunks, chunks[1:]):
            overlap_lines = prev.end_line - nxt.start_line + 1
            assert 45 <= overlap_lines <= 55

    def test_later_chunks_carry_structural_prefix_and_keep_offsets(self):
        content = "import os\nfrom x import y\n" + _lines(800)
        chunks = split_file(_file("a.py", content), per_file_cap=30_000)
        assert chunks[0].prefix == ""
        assert "import os" in chunks[1].prefix
        assert "not part of this chunk" in chunks[1].prefix
        # Prefix never changes the body start line.
        assert chunks[1].start_line > 1
        assert (
            chunks[1].body.splitlines()[0]
            == content.splitlines()[chunks[1].start_line - 1]
        )

    def test_chunk_size_never_exceeds_cap(self):
        content = "import os\n" + _lines(800)
        for chunk in split_file(_file("a.py", content), per_file_cap=30_000):
            assert chunk.size <= 30_000

    def test_single_huge_line_is_one_chunk(self):
        content = "x" * 50_000
        chunks = split_file(_file("bundle.min.js", content), per_file_cap=30_000)
        assert len(chunks) == 1
        assert chunks[0].body == content

    def test_split_is_deterministic(self):
        content = _lines(800)
        a = split_file(_file("a.py", content))
        b = split_file(_file("a.py", content))
        assert a == b


class TestPlan:
    def test_small_commit_single_batch(self):
        files = [_file(f"f{i}.py", "x" * 13_000) for i in range(3)]
        batches = plan_files(files, per_file_cap=30_000, batch_budget=200_000)
        assert len(batches) == 1
        assert batches[0].size == 39_000

    def test_large_scan_multiple_bounded_batches(self):
        files = [_file(f"f{i:03d}.py", "x" * 20_000) for i in range(70)]  # 1.4M chars
        batches = plan_files(files, per_file_cap=30_000, batch_budget=200_000)
        assert len(batches) == 7
        assert all(b.size <= 200_000 for b in batches)
        assert [b.index for b in batches] == list(range(7))

    def test_chunk_never_split_across_batches(self):
        files = [_file(f"f{i}.py", "x" * 150_000) for i in range(2)]
        batches = plan_files(files, per_file_cap=30_000, batch_budget=200_000)
        all_chunks = [c for b in batches for c in b.chunks]
        assert len(all_chunks) == sum(len(split_file(f, 30_000)) for f in files)
        assert all(b.size <= 200_000 for b in batches)

    def test_order_is_runtime_then_path_then_chunk(self):
        files = [
            _file("z.ts", "a", ("browser",)),
            _file("a.py", "b", ("server",)),
            _file("m.tsx", "c", ("mobile", "browser")),
        ]
        batches = plan_files(files)
        assert [c.path for c in batches[0].chunks] == ["z.ts", "m.tsx", "a.py"]

    def test_plan_is_deterministic(self):
        files = [_file(f"f{i}.py", "x" * (i * 1000)) for i in range(50)]
        assert plan_files(files) == plan_files(files)

    def test_batch_paths_unique_sorted(self):
        chunks = split_file(_file("big.py", _lines(800)))
        batch = Batch(index=0, chunks=tuple(chunks))
        assert batch.paths == ["big.py"]

    def test_file_content_independent_of_scan_size(self):
        target = _file("src/target.py", _lines(120))
        small = plan_files([target, _file("o.py", "x")])
        large = plan_files(
            [target] + [_file(f"o{i}.py", "x" * 5000) for i in range(400)]
        )
        small_chunk = next(
            c for b in small for c in b.chunks if c.path == "src/target.py"
        )
        large_chunk = next(
            c for b in large for c in b.chunks if c.path == "src/target.py"
        )
        assert small_chunk.body == large_chunk.body

    def test_empty_plan(self):
        assert plan([]) == []

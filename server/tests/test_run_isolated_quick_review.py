from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_isolated_quick_review.py"
SPEC = importlib.util.spec_from_file_location("run_isolated_quick_review", SCRIPT)
assert SPEC and SPEC.loader
review = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review
SPEC.loader.exec_module(review)


def test_safe_relative_paths_rejects_escape_and_absolute_paths() -> None:
    with pytest.raises(RuntimeError, match="unsafe candidate path"):
        review._safe_relative_paths(b"../outside\0")
    with pytest.raises(RuntimeError, match="unsafe candidate path"):
        review._safe_relative_paths(b"/outside\0")


def test_materialize_snapshot_copies_regular_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "snapshot"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file.txt").write_text("unchanged\n")

    review._materialize_snapshot(source, destination, (Path("nested/file.txt"),))

    assert (destination / "nested" / "file.txt").read_text() == "unchanged\n"


def test_materialize_snapshot_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "linked.txt").symlink_to(outside)

    with pytest.raises(RuntimeError, match="not a regular file"):
        review._materialize_snapshot(source, tmp_path / "snapshot", (Path("linked.txt"),))


def test_file_set_digest_changes_only_with_candidate_content(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.txt"
    ignored = tmp_path / "ignored.txt"
    candidate.write_text("before")
    ignored.write_text("before")
    files = (Path("candidate.txt"),)
    baseline = review._file_set_digest(tmp_path, files)

    ignored.write_text("after")
    assert review._file_set_digest(tmp_path, files) == baseline

    candidate.write_text("after")
    assert review._file_set_digest(tmp_path, files) != baseline

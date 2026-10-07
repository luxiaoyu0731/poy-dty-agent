from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    for command in ("git", "pre-commit", "semgrep", "gitleaks"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required review command is missing: {command}")

    source_files = _git_file_set(REPO_ROOT)
    review_files = [path for path in _changed_file_set(REPO_ROOT) if path in source_files]
    source_digest = _file_set_digest(REPO_ROOT, source_files)
    temporary_root = Path(tempfile.mkdtemp(prefix="poy-dty-quick-review-"))
    os.chmod(temporary_root, 0o700)
    snapshot = temporary_root / "snapshot"
    try:
        _materialize_snapshot(REPO_ROOT, snapshot, source_files)
        _run(["git", "init", "--quiet"], cwd=snapshot)
        _run(["git", "add", "--all"], cwd=snapshot)
        pre_commit = ["pre-commit", "run"]
        pre_commit.extend(["--files", *[path.as_posix() for path in review_files]] if review_files else ["--all-files"])
        result = subprocess.run(pre_commit, cwd=snapshot, check=False)
        if result.returncode != 0:
            return result.returncode
        result = subprocess.run(
            ["gitleaks", "dir", "--redact", "--no-banner", "--config", "config/security/gitleaks.toml", "."],
            cwd=snapshot,
            check=False,
        )
        return result.returncode
    finally:
        current_digest = _file_set_digest(REPO_ROOT, source_files)
        shutil.rmtree(temporary_root)
        if current_digest != source_digest:
            raise RuntimeError("source worktree changed during isolated quick review")
        print(
            f"isolated_review_source_unchanged=true files={len(source_files)} "
            f"review_paths={len(review_files)}"
        )


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _git_file_set(repo: Path) -> tuple[Path, ...]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return _safe_relative_paths(output)


def _changed_file_set(repo: Path) -> tuple[Path, ...]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return tuple(sorted(set(_safe_relative_paths(tracked + untracked)), key=lambda path: path.as_posix()))


def _safe_relative_paths(raw: bytes) -> tuple[Path, ...]:
    paths: list[Path] = []
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        path = Path(os.fsdecode(encoded))
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("git returned an unsafe candidate path")
        paths.append(path)
    return tuple(sorted(set(paths), key=lambda item: item.as_posix()))


def _materialize_snapshot(source: Path, destination: Path, files: tuple[Path, ...]) -> None:
    destination.mkdir(mode=0o700)
    for relative in files:
        source_path = source / relative
        if source_path.is_symlink() or not source_path.is_file():
            raise RuntimeError(f"review candidate is not a regular file: {relative.as_posix()}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)


def _file_set_digest(root: Path, files: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for relative in files:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"review candidate changed type: {relative.as_posix()}")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())

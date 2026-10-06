"""Generate paired deployment identities from the actual cloud source tree.

Run before building images, then publish the staged frontend identity after the
backend is healthy. No Git checkout, environment file, or database is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

INPUTS = (
    "server/app", "server/scripts", "server/contracts", "server/model_registry",
    "server/source_registry.json", "server/pyproject.toml", "server/uv.lock",
    "dist", "public/geo", "scripts", "Dockerfile", "docker-compose.yml",
    "docker-compose.prod.yml",
)


def build_identity(root: Path, git_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
        raise ValueError("a verified 40-character base git SHA is required")
    digest = hashlib.sha256()
    for name in INPUTS:
        base = root / name
        if not base.exists():
            raise FileNotFoundError(name)
        files = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in files:
            if not path.is_file() or path.name in {"release.json", ".DS_Store"}:
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    now = datetime.now(timezone.utc)
    content_hash = digest.hexdigest()
    return {
        "release_id": f"{now:%Y%m%dT%H%M%SZ}-{content_hash[:16]}",
        "release_hash": content_hash[:16],
        "content_sha256": content_hash,
        "created_at": now.isoformat(),
        "git_sha": git_sha,
        "source_tree_dirty": True,
        "provenance": "base_commit_plus_deployed_tree; content_sha256 identifies exact inputs",
    }


def write_identity(path: Path, metadata: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-git-sha", required=True)
    parser.add_argument("--frontend-output", type=Path, required=True)
    args = parser.parse_args()
    identity = build_identity(args.root.resolve(), args.base_git_sha)
    write_identity(args.root / "server/release.json", identity)
    write_identity(args.frontend_output, identity)
    print(json.dumps(identity))


if __name__ == "__main__":
    main()

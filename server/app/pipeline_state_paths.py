"""Shared read/write locations for pipeline reports and HTTP-attempt state."""

from __future__ import annotations

import os
from pathlib import Path


def shared_state_root(sqlite_path: str | Path) -> Path:
    override = os.getenv("PIPELINE_GRAPH_STATE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    parent = Path(sqlite_path).expanduser().resolve().parent
    # Docker stores /data/agent.db directly in the durable volume, whereas
    # the local layout is <state-root>/data/agent.db. Never choose / as a
    # writable state root merely because the Docker DB is one level higher.
    return parent if parent.parent == Path("/") else parent.parent


def local_production_directory(state_root: Path) -> Path:
    override = os.getenv("LOCAL_PRODUCTION_OUTPUT_DIR")
    return Path(override).expanduser().resolve() if override else state_root / "local-production"

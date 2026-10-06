"""Non-secret identity baked into an immutable release, never inferred from Git at runtime."""

from __future__ import annotations

import json
from pathlib import Path

RELEASE_FILE = Path(__file__).resolve().parents[1] / "release.json"


def read_release_identity() -> dict[str, str]:
    try:
        payload = json.loads(RELEASE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "unavailable"}
    if not isinstance(payload, dict):
        return {"status": "unavailable"}
    keys = ("release_id", "release_hash", "git_sha")
    if not all(isinstance(payload.get(key), str) and payload[key] for key in keys):
        return {"status": "unavailable"}
    return {"status": "available", **{key: payload[key] for key in keys}}


# Resolve from this process's immutable checkout once; a symlink switch must
# not make an old backend claim the new frontend's identity.
RUNTIME_RELEASE = read_release_identity()

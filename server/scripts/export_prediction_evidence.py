"""Export immutable public-evidence versions; requires explicit production authorization.

Always opens the explicitly selected database read-only. No app initialization,
migration, checkpoint, model invocation or mutation of forecast eligibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prediction_evidence_inputs import export_evidence_vintages  # noqa: E402


def write_new_private_file(destination: Path, content: bytes) -> None:
    parent = destination.parent.resolve(strict=True)
    fd, temporary = tempfile.mkstemp(prefix=".evidence-export-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)  # Exclusive publication; never overwrite.
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new file in an existing private directory")
    parser.add_argument("--as-of", default=None, help="defaults to current UTC; timestamps must carry a timezone")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    if args.output.exists() or args.output.is_symlink():
        parser.error("output must be a new file")
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)) as connection:
        exported = export_evidence_vintages(connection, as_of=args.as_of or datetime.now(UTC).isoformat())
    content = json.dumps(exported, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    write_new_private_file(args.output, content)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "rows": {kind: table["rows_exported"] for kind, table in exported["tables"].items()},
                "forecast_feature_approved": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

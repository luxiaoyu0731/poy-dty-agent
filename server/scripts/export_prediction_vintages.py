"""Bounded, read-only export. No model calls, migrations or raw document export.

Run from server/: python scripts/export_prediction_vintages.py --as-of ISO > export.json
Requires explicit authorization before pointing at a production database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prediction_inputs import LABEL_REGISTRY, digest, export_vintages, safe_url  # noqa: E402, F401


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--database", type=Path, help="defaults to configured database; always read-only")
    args = parser.parse_args()
    if args.database:
        path = args.database.resolve(strict=True)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        from app.storage import connect_readonly

        connection = connect_readonly()
    try:
        print(json.dumps(export_vintages(connection, as_of=args.as_of), ensure_ascii=False, allow_nan=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()

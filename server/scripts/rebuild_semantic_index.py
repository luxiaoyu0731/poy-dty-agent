from __future__ import annotations

import argparse
import json

from app.semantic_index import rebuild_semantic_index, semantic_index_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and atomically activate the versioned semantic RAG index.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    result = semantic_index_status() if args.status_only else rebuild_semantic_index(limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ready", "missing", "stale"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

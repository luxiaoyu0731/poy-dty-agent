from __future__ import annotations

import asyncio
import json

from app.rag_quality_eval import run_fixed_rag_quality_eval

if __name__ == "__main__":
    print(
        json.dumps(
            asyncio.run(run_fixed_rag_quality_eval()),
            ensure_ascii=False,
            indent=2,
        )
    )

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .foundation_utils import estimate_tokens, stable_hash

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{1,2}")


def tokenize(text: str) -> list[str]:
    tokens = [token.lower() for token in TOKEN_RE.findall(text or "")]
    return [token for token in tokens if token.strip()]


def chunk_text(text: str, *, max_chars: int = 900, overlap: int = 120) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]
    chunks: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + max_chars)
        window = cleaned[start:end]
        if end < len(cleaned):
            split_at = max(window.rfind("。"), window.rfind("；"), window.rfind("."), window.rfind(";"))
            if split_at > max_chars * 0.55:
                end = start + split_at + 1
                window = cleaned[start:end]
        chunks.append(window.strip())
        if end >= len(cleaned):
            break
        start = max(0, end - overlap)
    return chunks


def hashed_embedding(text: str, *, dimensions: int = 96) -> dict[str, float]:
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    buckets = [0.0] * dimensions
    for token, count in counts.items():
        index = int(stable_hash(token, length=8), 16) % dimensions
        buckets[index] += float(count)
    norm = math.sqrt(sum(value * value for value in buckets)) or 1.0
    return {str(index): round(value / norm, 6) for index, value in enumerate(buckets) if value}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    keys = set(left) & set(right)
    return sum(left[key] * right[key] for key in keys)


def chunk_record(document_id: str, index: int, text: str) -> dict[str, Any]:
    return {
        "chunk_id": f"chunk_{stable_hash({'document_id': document_id, 'index': index, 'text': text})}",
        "document_id": document_id,
        "chunk_index": index,
        "text": text,
        "token_est": estimate_tokens(text),
        "embedding": hashed_embedding(text),
    }

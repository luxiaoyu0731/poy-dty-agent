from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

SENSITIVE_KEYWORDS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "license",
    "login",
    "credential",
    "cookie",
    "authorization",
    "授权",
    "账号",
    "密码",
    "许可证",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_utc(value: object) -> datetime | None:
    """Parse an ISO timestamp and normalize offsets before comparisons."""
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_at_or_before(value: object, cutoff: object) -> bool:
    observed = parse_utc(value)
    boundary = parse_utc(cutoff)
    return bool(observed and boundary and observed <= boundary)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def json_dumps(value: Any) -> str:
    return json.dumps(sanitize_payload(value), ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def stable_hash(value: Any, length: int = 16) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                cleaned[key_text] = "[redacted]"
            else:
                cleaned[key_text] = sanitize_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def safe_summary(value: Any, *, max_chars: int = 280) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(sanitize_payload(value), ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", _redact_sensitive_text(text)).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(word in normalized for word in SENSITIVE_KEYWORDS)


def _redact_sensitive_text(text: str) -> str:
    redacted = text
    redacted = re.sub(
        r"(?i)(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+",
        r"\1=[redacted]",
        redacted,
    )
    redacted = re.sub(r"(?i)(authorization|cookie)\s*[:=]\s*['\"]?[^'\"\\n]+", r"\1=[redacted]", redacted)
    redacted = re.sub(r"(账号|密码|密钥|许可证)\s*[:：]\s*[^，。；;\n]+", r"\1：[redacted]", redacted)
    return redacted

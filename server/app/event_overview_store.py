"""Durable, bounded overview generation; no primary database writes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .event_overview import (
    PROMPT_VERSION as OVERVIEW_PROMPT_VERSION,
)
from .event_overview import (
    OverviewValidationError,
    generate_title_overview,
    validate_overview,
)

# USD millionths per token, peak rate verified 2026-09-07. Conservative
# non-cache input rate is used even on cache hits. No unknown models allowed.
INPUT_RATE = 1.32
OUTPUT_RATE = 3.96
MAX_OUTPUT = 512
EVENT_OVERVIEW_LEDGER_STAGE = "event_overview"


def _ledger_business_date() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _settle_amount_microusd(usage: object) -> int | None:
    """Mirror settle() arithmetic; None when usage is not a usable metering."""
    if not isinstance(usage, dict):
        return None
    values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    if not all(type(value) is int and value >= 0 for value in values):  # noqa: E721
        return None
    return math.ceil(values[0] * INPUT_RATE + values[1] * OUTPUT_RATE)


def _record_overview_call(
    *,
    usage: object,
    latency_ms: int,
    prompt_version: str,
    error: str | None,
) -> None:
    """Layer-1 ledger row for the event-overview LLM touchpoint (micro-USD).

    Best-effort: the overview path (budget fail-closed, lease, settle) must
    keep its semantics even if the observability ledger hiccups.
    """
    try:
        from .storage import record_llm_call

        tokens = usage if isinstance(usage, dict) else {}
        prompt_tokens = int(tokens.get("prompt_tokens") or 0)  # type: ignore[arg-type]
        completion_tokens = int(tokens.get("completion_tokens") or 0)  # type: ignore[arg-type]
        amount = _settle_amount_microusd(usage)
        record_llm_call(
            trace_id=f"event_overview:{uuid4().hex[:8]}",
            provider="deepseek",
            model="deepseek-v4-pro",
            stage=EVENT_OVERVIEW_LEDGER_STAGE,
            business_date=_ledger_business_date(),
            question="",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_source="provider_usage" if amount is not None else None,
            prompt_version=prompt_version,
            # micro-USD by design (budget.json arithmetic); currency unification
            # stays a tracked batch-2 open item (VERTICAL-SLICE §6-5).
            cost_micros=amount,
            fallback=error is not None,
            error=error,
            evidence_level="internal",
            confidence=0.0,
        )
    except Exception:  # noqa: BLE001 - observability must not break the overview path.
        pass


def store_root() -> Path:
    return Path(os.environ.get("SQLITE_PATH", "data/agent.db")).resolve().parent / "event-overviews"


def title_key(title: str) -> str:
    return hashlib.sha256(title.encode()).hexdigest()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".overview-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def budget_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".budget.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def initialize_budget(root: Path, *, limit_microusd: int, initial_microusd: int = 50_000) -> None:
    if not 0 <= initial_microusd <= limit_microusd <= 10_000_000:
        raise ValueError("invalid_budget")
    with budget_lock(root):
        if (root / "budget.json").exists():
            raise ValueError("budget_already_initialized")
        atomic_json(
            root / "budget.json",
            {
                "limit_microusd": limit_microusd,
                "used_microusd": initial_microusd,
                "initial_trial_reserve_microusd": initial_microusd,
                "reservations": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
        )


def reserve(root: Path, title: str) -> str:
    # Two serializations of arbitrary title are generously bounded by 12 bytes
    # per input byte plus 4096 bytes of instruction/protocol allowance. Token
    # counts cannot exceed byte counts for this tokenizer. Budget is deliberately
    # over-reserved; crashes keep the reservation charged.
    maximum = math.ceil((12 * len(title.encode()) + 4096) * INPUT_RATE + MAX_OUTPUT * OUTPUT_RATE)
    with budget_lock(root):
        data = json.loads((root / "budget.json").read_text())
        if sum(item["title_hash"] == title_key(title) for item in data["reservations"].values()) >= 3:
            raise ValueError("overview_attempts_exhausted")
        if data["used_microusd"] + maximum > data["limit_microusd"]:
            raise ValueError("overview_budget_exhausted")
        key = uuid4().hex
        data["used_microusd"] += maximum
        data["reservations"][key] = {"reserved": maximum, "status": "reserved", "title_hash": title_key(title)}
        atomic_json(root / "budget.json", data)
        return key


def settle(root: Path, key: str, usage: dict | None) -> None:
    # Unknown/broken usage never refunds a paid attempt.
    if not isinstance(usage, dict):
        return
    values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    if not all(type(value) is int and value >= 0 for value in values):
        return
    amount = math.ceil(values[0] * INPUT_RATE + values[1] * OUTPUT_RATE)
    with budget_lock(root):
        data = json.loads((root / "budget.json").read_text())
        entry = data["reservations"][key]
        if entry["status"] != "reserved":
            return
        # Unexpected larger metering fails closed for subsequent calls.
        if amount > entry["reserved"]:
            data["limit_microusd"] = 0
        data["used_microusd"] += amount - entry["reserved"]
        entry.update(status="settled", charged=amount, usage=usage)
        atomic_json(root / "budget.json", data)


def read_overview(title: str, root: Path | None = None) -> dict | None:
    root = root or store_root()
    try:
        payload = json.loads((root / f"{title_key(title)}.json").read_text())
        if payload.get("original_title", payload["source_title"]) != title:
            return None
        validate_overview(
            {"source_title": payload["source_title"], "overview_zh": payload["overview_zh"]}, payload["source_title"]
        )
        if payload.get("basis") != "title" or payload.get("title_sha256") != title_key(title):
            return None
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        return None


def recover_paid_overviews(root: Path) -> int:
    """Revalidate retained paid outputs after deterministic validator fixes."""
    recovered = 0
    for path in sorted((root / "failures").glob("*.json")):
        try:
            saved = json.loads(path.read_text())
            title = saved["title"]
            if read_overview(title, root):
                continue
            response = saved["response"]
            payload = json.loads(response["choices"][0]["message"]["content"])
            if not isinstance(payload, dict) or set(payload) != {"overview_zh"}:
                continue
            result = validate_overview({"source_title": title, **payload}, title)
            atomic_json(
                root / f"{title_key(title)}.json",
                {
                    **result.model_dump(),
                    "basis": "title",
                    "title_sha256": title_key(title),
                    "prompt_version": "paid-output-revalidated-v2",
                    "usage": response.get("usage", {}),
                    "retained_response": path.name,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            recovered += 1
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return recovered


async def repair_encoded_title(title: str, source_url: str, root: Path) -> dict:
    parsed = urlparse(source_url)
    if parsed.netloc != "news.cnpc.com.cn" or not re.fullmatch(r"/system/\d{4}/\d{2}/\d{2}/\d+\.shtml", parsed.path):
        return {"status": "invalid_title"}
    from .news import _fetch_text

    html, _ = await _fetch_text(source_url)
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return {"status": "invalid_title"}
    corrected = unescape(match[1]).strip()
    text = re.sub(r"(?:-中国石油新闻中心)+$", "", corrected)
    if "\ufffd" in corrected:
        return {"status": "invalid_title"}
    validate_overview({"source_title": corrected, "overview_zh": text}, corrected)
    atomic_json(
        root / f"{title_key(title)}.json",
        {
            "source_title": corrected,
            "original_title": title,
            "overview_zh": text,
            "basis": "title",
            "title_sha256": title_key(title),
            "source_url": source_url,
            "decoded_html_sha256": title_key(html),
            "prompt_version": "source-title-decoding-v1",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )
    return {"status": "source_title_decoded"}


async def ensure_overview(title: str, *, root: Path | None = None, client=None, source_url: str = "") -> dict:
    root = root or store_root()
    cached = read_overview(title, root)
    if cached:
        return {"status": "cached"}
    if not (root / "budget.json").is_file():
        return {"status": "disabled"}
    if "\ufffd" in title:
        return await repair_encoded_title(title, source_url, root)
    if not title.strip() or len(title) > 2000:
        return {"status": "invalid_title"}
    # Nonblocking cross-process lease: a duplicate worker never spends twice.
    with (root / f".{title_key(title)}.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "leased"}
        try:
            if read_overview(title, root):
                return {"status": "cached"}
            if client is None:
                from .deepseek_client import DeepSeekClient

                client = DeepSeekClient()
            if client.model != "deepseek-v4-pro" or not client.api_key:
                return {"status": "model_configuration_unsupported"}
            client.max_retries = 0
            client.max_output_tokens = MAX_OUTPUT
            client.set_http_attempt_budget(1)
            key = reserve(root, title)
            try:
                started = time.perf_counter()
                result = await generate_title_overview(client, title=title)
                _record_overview_call(
                    usage=result.get("usage"),
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    prompt_version=str(result.get("prompt_version") or ""),
                    error=None,
                )
            except OverviewValidationError as exc:
                _record_overview_call(
                    usage=exc.response.get("usage"),
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    prompt_version=OVERVIEW_PROMPT_VERSION,
                    error="overview_validation_failed",
                )
                settle(root, key, exc.response.get("usage"))
                atomic_json(root / "failures" / f"{key}.json", {"title": title, "response": exc.response})
                return {"status": "validation_failed"}
            except (Exception, asyncio.CancelledError):
                # Crash, cancellation and unknown provider outcomes retain all
                # reserved cost; no success is written.
                raise
            settle(root, key, result.get("usage"))
            result["generated_at"] = datetime.now(UTC).isoformat()
            atomic_json(root / f"{title_key(title)}.json", result)
            return {"status": "generated"}
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)

"""Paired replay port: sealed HTTP receipts, no production traces or state."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from datetime import date
from pathlib import Path

from scripts.experiments.campaign_budget import PRICE_BOOK, atomic


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class CachedReplayJsonPort:
    def __init__(self, client, cache_dir: Path, *, attempt_cap: int = 40):
        if type(attempt_cap) is not int or attempt_cap <= 0:
            raise ValueError("invalid_replay_attempt_cap")
        if client.model != "deepseek-v4-pro":
            raise ValueError("frozen_replay_model_required")
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache_dir.is_symlink():
            raise ValueError("replay_cache_symlink")
        self.client, self.model, self.cache_dir, self.cap = (
            client,
            client.model,
            cache_dir,
            attempt_cap,
        )
        self.day = None
        self.used = 0
        self.new_requests = 0
        self.cache_hits = 0
        self.models = set()
        self._serial = asyncio.Lock()

    def bind_business_date(self, business_date: str):
        date.fromisoformat(business_date)
        if self.day is not None and self.day != business_date:
            raise ValueError("replay_port_day_changed")
        self.day = business_date

    def budget_snapshot(self):
        return {
            "attempts_reserved": self.used,
            "cap": self.cap,
            "basis": "模拟 HTTP 尝试预留（含缓存复用）",
            "source": "paired-replay-cache:attempt_receipts",
            "business_date": self.day,
            "new_request_invocations": self.new_requests,
            "cached_attempts": self.cache_hits,
            "returned_models": sorted(self.models),
        }

    async def complete_json(
        self, *, stage: str, business_date: str, system: str, user: str
    ):
        self.bind_business_date(business_date)
        async with self._serial:
            if self.used >= self.cap:
                raise RuntimeError("replay_http_attempt_cap_reached")
            self.used += 1
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            request = {
                "policy": "paired-replay-json-port.v1",
                "stage": stage,
                "business_date": business_date,
                "model": self.model,
                "messages": messages,
                "output_cap": self.client.output_cap,
            }
            identity = digest(request)
            path = self.cache_dir / f"{identity}.json"
            lock_fd = os.open(
                self.cache_dir / f"{identity}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
                if path.is_symlink():
                    raise ValueError("replay_receipt_symlink")
                if path.exists():
                    packet = json.loads(path.read_text())
                    body = {k: v for k, v in packet.items() if k != "receipt_sha256"}
                    if (
                        packet.get("receipt_sha256") != digest(body)
                        or packet.get("request") != request
                    ):
                        raise ValueError("replay_receipt_changed")
                    self.cache_hits += 1
                else:
                    packet = {"request": request, "state": "pending"}
                    atomic(path, {**packet, "receipt_sha256": digest(packet)})
                    self.new_requests += 1
                    try:
                        response = await self.client._post_chat_completion(
                            messages, json_mode=True
                        )
                    except Exception as exc:
                        packet = {
                            **packet,
                            "state": "failed",
                            "error_type": type(exc).__name__,
                        }
                        atomic(path, {**packet, "receipt_sha256": digest(packet)})
                        raise
                    # Seal the paid response before parsing, so a schema error
                    # or interrupted runner never silently buys it again.
                    packet = {**packet, "state": "response", "response": response}
                    atomic(path, {**packet, "receipt_sha256": digest(packet)})
                if packet["state"] != "response":
                    raise RuntimeError("cached_failed_or_indeterminate_attempt")
                response = packet["response"]
                model = response.get("model") if isinstance(response, dict) else None
                if model not in PRICE_BOOK:
                    raise ValueError("unexpected_replay_response_model")
                self.models.add(model)
                from app.agent_chain import parse_chain_json

                output = parse_chain_json(self.client._completion_content(response))
                usage = response.get("usage") or {}
                output["_cost"] = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                }
                return output
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

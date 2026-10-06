"""Shared 350 CNY campaign ledger, including legacy reserve and failed HTTP.

Before HTTP reserve a UTF-8-byte input bound plus output cap. Only verified
provider usage settles the reserve to a conservative peak-price upper bound;
missing/invalid usage, wrong model, failures and interruptions retain it.
No actual invoice is claimed. All amounts are rounded upward to micros.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from urllib.parse import urlparse

PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICE_BOOK = {"deepseek-v4-pro": (Decimal("9"), Decimal("27")),
              "deepseek-v4-pro-0813": (Decimal("9"), Decimal("27"))}
MICROS = Decimal(1_000_000)


def micros(value) -> int:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("invalid_campaign_amount")
    return int((number * MICROS).to_integral_value(rounding=ROUND_CEILING))


def atomic(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".campaign-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CampaignBudget:
    def __init__(self, directory: Path):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("existing_private_budget_directory_required")
        self.path = directory / "budget.json"
        self.lock = directory / ".budget.lock"
        if self.path.is_symlink() or self.lock.is_symlink() or not self.path.is_file():
            raise ValueError("existing_cumulative_budget_required")

    def _read(self) -> dict:
        data = json.loads(self.path.read_text())
        cap = micros(data["cap_cny"])
        if not 0 < cap <= 350_000_000 or type(data.get("attempts")) is not int or data["attempts"] < 0:
            raise ValueError("invalid_campaign_cap_or_attempts")
        current = micros(data["reserved_cny"])
        if current > cap:
            raise ValueError("campaign_cap_already_exceeded")
        records = data.setdefault("campaign_attempts", {})
        if "legacy_reserved_micros" not in data:
            data["legacy_reserved_micros"] = current
        expected = data["legacy_reserved_micros"] + sum(r["held_micros"] for r in records.values())
        if expected != current:
            raise ValueError("campaign_ledger_changed_outside_meter")
        return data

    def reserve(self, amount, *, request_sha256: str) -> str:
        amount_micros = micros(amount)
        if amount_micros <= 0:
            raise ValueError("positive_reservation_required")
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            current = micros(data["reserved_cny"])
            if current + amount_micros > micros(data["cap_cny"]):
                raise RuntimeError("campaign_budget_exhausted")
            key = uuid.uuid4().hex
            data["campaign_attempts"][key] = {"state": "reserved_or_unknown", "held_micros": amount_micros,
                "reserved_micros": amount_micros, "request_sha256": request_sha256}
            data.update(reserved_cny=float(Decimal(current + amount_micros) / MICROS), attempts=data["attempts"] + 1)
            atomic(self.path, data)
            return key

    def settle(self, key: str, response: dict) -> bool:
        usage = response.get("usage")
        model = response.get("model")
        if model not in PRICE_BOOK or not isinstance(usage, dict):
            return False
        fields = (usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens"))
        if any(type(v) is not int or v < 0 for v in fields) or fields[0] + fields[1] != fields[2] or fields[2] == 0:
            return False
        input_rate, output_rate = PRICE_BOOK[model]
        cost = micros((Decimal(fields[0]) * input_rate + Decimal(fields[1]) * output_rate) / MICROS)
        digest = hashlib.sha256(json.dumps({"model": model, "usage": usage}, sort_keys=True).encode()).hexdigest()
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            record = data["campaign_attempts"][key]
            if record["state"] == "settled_peak_upper":
                if record["usage_sha256"] != digest:
                    raise ValueError("campaign_usage_changed")
                return True
            if cost > record["reserved_micros"]:
                raise ValueError("provider_usage_exceeded_reservation")
            total = micros(data["reserved_cny"]) - record["held_micros"] + cost
            record.update(state="settled_peak_upper", held_micros=cost, usage_sha256=digest,
                          model=model, usage=usage, price_source=PRICE_SOURCE,
                          input_cny_per_million=str(input_rate), output_cny_per_million=str(output_rate))
            data["reserved_cny"] = float(Decimal(total) / MICROS)
            atomic(self.path, data)
            return True


class MeteredJsonClient:
    """Adapter for the existing client; preserves its daily HTTP attempt guard."""
    def __init__(self, client, budget: CampaignBudget, *, output_cap: int = 2500):
        if type(output_cap) is not int or not 1 <= output_cap <= 2500:
            raise ValueError("bounded_output_required")
        provider = urlparse(client.base_url)
        if provider.scheme != "https" or provider.netloc != "api.deepseek.com" or provider.path not in {"", "/v1"}:
            raise ValueError("campaign_direct_provider_required")
        if client.model != "deepseek-v4-pro":
            raise ValueError("frozen_campaign_model_required")
        self.client, self.budget, self.model = client, budget, client.model
        client.max_retries = 0
        client.max_output_tokens = output_cap
        self.output_cap = output_cap

    def set_http_attempt_budget(self, limit, *, on_attempt=None):
        self.client.set_http_attempt_budget(limit, on_attempt=on_attempt)

    def _completion_content(self, response):
        return self.client._completion_content(response)

    async def _post_chat_completion(self, messages, *, json_mode=False):
        if not json_mode:
            raise ValueError("campaign_json_mode_required")
        request = json.dumps({"model": self.model, "messages": messages, "max_tokens": self.output_cap,
                              "thinking": {"type": "disabled"}}, ensure_ascii=False, sort_keys=True).encode()
        # Bytes bound tokenizer input conservatively; add protocol overhead.
        reserve = (Decimal(len(request) + 2048) * 9 + Decimal(self.output_cap) * 27) / MICROS
        request_hash = hashlib.sha256(request).hexdigest()
        previous = self.client.http_attempt_callback
        attempts = []
        def before_http(index):
            key = self.budget.reserve(reserve, request_sha256=request_hash)
            attempts.append(key)
            if previous:
                previous(index)
        self.client.http_attempt_callback = before_http
        try:
            response = await self.client._post_chat_completion(messages, json_mode=True)
            if len(attempts) != 1:
                raise ValueError("single_metered_http_attempt_required")
            self.budget.settle(attempts[0], response)
            return response
        finally:
            self.client.http_attempt_callback = previous

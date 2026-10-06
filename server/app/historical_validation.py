from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .storage import connect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = (PROJECT_ROOT / ".codex-run").resolve()


def _eligible(p: dict[str, Any]) -> bool:
    train, test, score = p["train_window"], p["test_window"], p["scoring"]
    if not all((train.get("start"), train.get("end"), test.get("start"), test.get("end"))):
        return False
    return bool(
        p["trust_level"] == "strict_reproducible"
        and p["validation_layer"] == "strict"
        and date.fromisoformat(train["end"]) < date.fromisoformat(test["start"])
        and score["total"] > 0
        and score["scored"] == score["total"]
        and abs(score["coverage"] - score["scored"] / score["total"]) < 1e-9
        and p["leakage"]["status"] == "passed"
        and not p["flags"]["posthoc"]
        and not p["flags"]["small_sample"]
        and p["limitations"]
        and p["lineage"]["generator"]
        and p["lineage"]["rebuild_command"]
        and p["lineage"]["input_hashes"]
        and p["lineage"]["config_hash"]
        and p.get("visibility_audit", {}).get("status") == "passed"
        and p.get("lineage", {}).get("manifest", {}).get("status") == "verified"
    )


def register_asset(payload: dict[str, Any]) -> dict[str, Any]:
    artifact = (PROJECT_ROOT / payload["lineage"]["artifact_path"]).resolve()
    if not artifact.is_relative_to(ASSET_ROOT) or not artifact.is_file():
        raise ValueError("artifact_path must reference an existing file under .codex-run")
    actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    supplied_hash = payload["lineage"].get("artifact_sha256")
    if supplied_hash and supplied_hash != actual_hash:
        raise ValueError("artifact_sha256 does not match artifact content")
    payload = json.loads(json.dumps(payload))
    payload["asset_id"] = payload.get("asset_id") or f"hva-{uuid4().hex[:12]}"
    payload["registered_at"] = datetime.now(UTC).isoformat()
    payload["lineage"]["artifact_sha256"] = actual_hash
    payload["historical_validation_eligible"] = _eligible(payload)
    if not payload["historical_validation_eligible"]:
        reasons: list[str] = []
        if payload["leakage"]["status"] != "passed":
            reasons.append(f"leakage:{payload['leakage']['status']}")
        if payload["flags"]["posthoc"]:
            reasons.append("posthoc")
        if payload["flags"]["small_sample"]:
            reasons.append("small_sample")
        if payload["validation_layer"] != "strict":
            reasons.append("diagnostic_layer")
        payload["eligibility_reasons"] = reasons or ["strict_reproducibility_requirements_not_met"]
    else:
        payload["eligibility_reasons"] = []
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO historical_validation_assets(
                asset_id, name, registered_at, payload, visibility_audit, reproduction_manifest
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                payload["asset_id"],
                payload["name"],
                payload["registered_at"],
                json.dumps(payload, ensure_ascii=False),
                json.dumps(payload.get("visibility_audit", {}), ensure_ascii=False),
                json.dumps(payload.get("lineage", {}).get("manifest", {}), ensure_ascii=False),
            ),
        )
    return payload


def list_assets() -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            "SELECT payload FROM historical_validation_assets ORDER BY registered_at DESC"
        ).fetchall()
    items = [json.loads(row["payload"]) for row in rows]
    return {
        "items": items,
        "summary": {
            "strict_eligible_count": sum(bool(x["historical_validation_eligible"]) for x in items),
            "diagnostic_count": sum(x["validation_layer"] == "diagnostic" for x in items),
            "total_count": len(items),
        },
    }


def update_asset_governance(asset_id: str, governance: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        row = connection.execute(
            "SELECT payload FROM historical_validation_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if not row:
            raise KeyError("asset not found")
        payload = json.loads(row["payload"])
        visibility = governance.get("visibility_audit", {})
        required = (
            "source_publish_time",
            "first_seen_at",
            "visible_at",
            "revision",
            "ingested_at",
            "status",
            "caveats",
        )
        if visibility and any(key not in visibility for key in required):
            raise ValueError(
                "visibility_audit must include publish/first_seen/visible/revision/ingested/status/caveats"
            )
        if visibility:
            payload["visibility_audit"] = visibility
        manifest = governance.get("manifest")
        if manifest is not None:
            payload.setdefault("lineage", {})["manifest"] = manifest
        payload["historical_validation_eligible"] = _eligible(payload)
        connection.execute(
            """
            UPDATE historical_validation_assets
            SET payload=?, visibility_audit=?, reproduction_manifest=?
            WHERE asset_id=?
            """,
            (
                json.dumps(payload, ensure_ascii=False),
                json.dumps(payload.get("visibility_audit", {}), ensure_ascii=False),
                json.dumps(payload.get("lineage", {}).get("manifest", {}), ensure_ascii=False),
                asset_id,
            ),
        )
    return payload

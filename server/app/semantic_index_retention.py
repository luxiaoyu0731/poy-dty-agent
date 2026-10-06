"""Automatic retention policy for versioned semantic index generations.

Every corpus change produces a new full index generation (~100 MB of vectors in
production). Without an upper bound the generations grow indefinitely and
recreate the disk crisis cleaned up on 2026-09-17 (39 generations, 5.2 GiB
database; see ``agent-context/agent-refactor-20260917/INDEX-CLEANUP.md``).

After each successful activation the policy keeps the newest
``NON_ACTIVE_KEEP`` non-active generations (plus the active one) and prunes the
rest. Pruning is anchored: an online-backup snapshot of the database is taken
first and must pass ``PRAGMA integrity_check`` before any row is deleted; the
anchor directory itself rotates and keeps the newest ``ANCHOR_KEEP`` snapshots.
Anchors are throttled to at most one per ``ANCHOR_MIN_AGE_DAYS`` days (default
7): while a younger anchor exists the prune is deferred with status ``skipped``
(``skipped:weekly_anchor_active`` in the audit log) and retried on a later
activation. The policy is fail-open: any failure is recorded in the audit log and retried
on the next activation, and the freshly activated generation always wins.

Guardrails:
- The active generation and the ``building_index_id`` pointer are never pruned.
- Generations in ``building`` or ``failed`` status are never pruned (a failed
  generation is an operator-inspectable artifact, not garbage).
- Every DELETE re-checks protection inside the pruning transaction, so even a
  stale plan cannot delete a generation that concurrently became protected.
- At most ``MAX_PRUNE_PER_RUN`` generations are removed per invocation.
- After pruning, the active generation's chunk count must be unchanged and the
  FTS table must not reference deleted chunks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import (
    configured_db_path,
    connect,
    connect_serialized,
    remove_sqlite_artifacts,
    secure_private_directory,
    secure_sqlite_artifacts,
)

ANCHOR_DIR_NAME = "index-prune-anchors"
PRUNE_LOG_NAME = "prune-log.jsonl"
PROTECTED_STATUSES = ("building", "failed")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


NON_ACTIVE_KEEP = max(0, _env_int("SEMANTIC_INDEX_RETENTION_KEEP", 3))
ANCHOR_KEEP = max(1, _env_int("SEMANTIC_INDEX_RETENTION_ANCHOR_KEEP", 2))
MAX_PRUNE_PER_RUN = max(0, _env_int("SEMANTIC_INDEX_RETENTION_MAX_PRUNE", 10))
# Final backup state (DISK-MODEL §7): a prune anchor (a full database copy) is
# only taken when the newest existing anchor is at least this many days old.
# Younger anchor -> the prune is skipped entirely for now (status "skipped",
# reason "weekly_anchor_active" in the audit log) and retried on a later
# activation, keeping the steady state at roughly one prune anchor per week.
ANCHOR_MIN_AGE_DAYS = max(0, _env_int("SEMANTIC_INDEX_RETENTION_ANCHOR_MIN_AGE_DAYS", 7))

# Protection is re-evaluated inside the pruning transaction: a generation that
# became active (or the new building target) between planning and deletion is
# spared by the DELETE itself, not only by the plan.
_PROTECTION_CLAUSE = (
    "index_id NOT IN ("
    "SELECT active_index_id FROM semantic_index_state WHERE state_key='default' "
    "UNION ALL "
    "SELECT building_index_id FROM semantic_index_state "
    "WHERE state_key='default' AND building_index_id!='')"
)


class _PruneAborted(RuntimeError):
    """Pruning was stopped before any row was deleted; nothing was changed."""


def prune_non_active_indices(
    *,
    trigger: str = "",
    dry_run: bool = False,
    keep: int | None = None,
    max_prune: int | None = None,
    anchor_keep: int | None = None,
) -> dict[str, Any]:
    """Apply the generation retention policy; never raises for expected faults.

    Returns a structured report (also appended to the anchor directory's
    ``prune-log.jsonl``). ``status`` is one of ``not_needed``, ``dry_run``,
    ``pruned``, ``aborted`` (stopped safely before deleting) or ``failed``.
    """
    started = time.monotonic()
    keep = NON_ACTIVE_KEEP if keep is None else max(0, keep)
    max_prune = MAX_PRUNE_PER_RUN if max_prune is None else max(0, max_prune)
    anchor_keep = ANCHOR_KEEP if anchor_keep is None else max(1, anchor_keep)
    db_path = configured_db_path()
    anchor_dir = db_path.parent / "backups" / ANCHOR_DIR_NAME
    report: dict[str, Any] = {
        "trigger": trigger,
        "started_at": datetime.now(UTC).isoformat(),
        "policy": {"non_active_keep": keep, "max_prune_per_run": max_prune, "anchor_keep": anchor_keep},
        "anchor_dir": str(anchor_dir),
    }
    try:
        plan = _select_prune_plan(keep=keep, max_prune=max_prune)
        report.update(
            active_index_id=plan["active_index_id"],
            building_index_id=plan["building_index_id"],
            kept_index_ids=plan["kept_index_ids"],
            pruned_index_ids=[],
            skipped_index_ids=[],
            pruned_counts={"indices": 0, "documents": 0, "chunks": 0, "fts": 0},
            active_chunk_count_before=plan["active_chunk_count"],
            prune_capped=plan["capped"],
        )
        if not plan["prunable_index_ids"]:
            report["status"] = "not_needed"
            return report
        if dry_run:
            report["status"] = "dry_run"
            report["pruned_index_ids"] = plan["prunable_index_ids"]
            return report
        anchor_age = _newest_anchor_age_seconds(anchor_dir, db_path.stem)
        min_age_seconds = ANCHOR_MIN_AGE_DAYS * 86400
        if anchor_age is not None and min_age_seconds > 0 and anchor_age < min_age_seconds:
            # Weekly anchor throttle (DISK-MODEL §7): a fresh prune anchor already
            # exists, so this prune is deferred; the audit log records
            # skipped:weekly_anchor_active and the next activation retries.
            report.update(
                status="skipped",
                skipped="weekly_anchor_active",
                anchor_age_seconds=round(anchor_age, 3),
                anchor_min_age_seconds=min_age_seconds,
            )
            return report
        anchor = _create_anchor(db_path, anchor_dir)
        report["anchor"] = {
            "path": str(anchor["path"]),
            "size_bytes": anchor["size_bytes"],
            "integrity": "ok",
            "elapsed_seconds": anchor["elapsed_seconds"],
        }
        deleted = _delete_generations(plan["prunable_index_ids"])
        report.update(
            status="pruned",
            pruned_index_ids=deleted["pruned_index_ids"],
            skipped_index_ids=deleted["skipped_index_ids"],
            pruned_counts=deleted["counts"],
        )
        removed, kept = _rotate_anchors(anchor_dir, db_path.stem, anchor_keep)
        report["anchors_rotated_out"] = removed
        report["anchors_kept"] = kept
        verification = _verify_after_prune(plan["active_index_id"], plan["active_chunk_count"])
        report.update(verification)
        if verification.get("alert"):
            report["status"] = "pruned_with_alert"
        return report
    except _PruneAborted as exc:
        report.update(status="aborted", reason=str(exc))
        return report
    except Exception as exc:  # noqa: BLE001 -- fail-open: the policy must never break activation
        report.update(status="failed", error=f"{type(exc).__name__}:{exc}")
        return report
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at"] = datetime.now(UTC).isoformat()
        _append_prune_log(anchor_dir, report)


def _select_prune_plan(*, keep: int, max_prune: int) -> dict[str, Any]:
    """Read the registry and compute which non-active generations may be pruned."""
    with closing(connect()) as connection, connection:
        state = connection.execute(
            "SELECT active_index_id, building_index_id FROM semantic_index_state WHERE state_key='default'"
        ).fetchone()
        active_index_id = str(state["active_index_id"]) if state else ""
        building_index_id = str(state["building_index_id"] or "") if state else ""
        if not active_index_id:
            raise _PruneAborted("no_active_index")
        clauses = ["index_id != ?", "status NOT IN (?, ?)"]
        params: list[Any] = [active_index_id, *PROTECTED_STATUSES]
        if building_index_id:
            clauses.append("index_id != ?")
            params.append(building_index_id)
        rows = connection.execute(
            "SELECT index_id FROM semantic_indices "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, rowid DESC",
            params,
        ).fetchall()
        candidates = [str(row["index_id"]) for row in rows]
        prunable = candidates[keep:]
        capped = False
        if len(prunable) > max_prune:
            prunable = prunable[:max_prune]
            capped = True
        active_chunk_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_chunks WHERE index_id=?", (active_index_id,)
            ).fetchone()[0]
        )
        protected_rows = connection.execute(
            "SELECT index_id FROM semantic_indices WHERE status IN (?, ?) OR index_id IN (?, ?)",
            (*PROTECTED_STATUSES, active_index_id, building_index_id or active_index_id),
        ).fetchall()
    return {
        "active_index_id": active_index_id,
        "building_index_id": building_index_id,
        "kept_index_ids": candidates[:keep],
        "prunable_index_ids": prunable,
        "protected_index_ids": sorted({str(row["index_id"]) for row in protected_rows}),
        "active_chunk_count": active_chunk_count,
        "capped": capped,
    }


def _create_anchor(db_path: Path, anchor_dir: Path) -> dict[str, Any]:
    """Snapshot the database via the online backup API; abort unless verified."""
    if not db_path.is_file():
        raise _PruneAborted(f"database_missing:{db_path}")
    anchor_dir.mkdir(parents=True, exist_ok=True)
    secure_private_directory(anchor_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{db_path.stem}.index-prune-anchor.{stamp}"
    anchor_path = anchor_dir / f"{prefix}.sqlite"
    counter = 1
    while anchor_path.exists():
        anchor_path = anchor_dir / f"{prefix}.{counter}.sqlite"
        counter += 1
    started = time.monotonic()
    target: sqlite3.Connection | None = None
    try:
        with closing(connect()) as source:
            target = connect_serialized(anchor_path)
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
            target.close()
            target = None
        if not check or str(check[0]) != "ok":
            raise _PruneAborted(f"anchor_integrity_failed:{check[0] if check else 'no_result'}")
        secure_sqlite_artifacts(anchor_path)
    except _PruneAborted:
        with suppress(OSError):
            remove_sqlite_artifacts(anchor_path)
        raise
    except BaseException:
        if target is not None:
            with suppress(Exception):
                target.close()
        with suppress(OSError):
            remove_sqlite_artifacts(anchor_path)
        raise
    return {
        "path": anchor_path,
        "size_bytes": anchor_path.stat().st_size,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _delete_generations(prune_index_ids: list[str]) -> dict[str, Any]:
    """Delete pruned generations from all four tables in one transaction.

    Statement order (FTS -> chunks -> documents -> registry) and the explicit
    FTS maintenance mirror the verified 2026-09-17 cleanup surgery. Each
    statement carries the live protection clause, so a generation that became
    active/building between planning and deletion loses zero rows.
    """
    counts = {"indices": 0, "documents": 0, "chunks": 0, "fts": 0}
    pruned: list[str] = []
    skipped: list[str] = []
    with closing(connect()) as connection:
        with connection:  # single transaction: all four tables or nothing
            for index_id in prune_index_ids:
                fts = connection.execute(
                    f"DELETE FROM semantic_chunks_fts WHERE index_id=? AND {_PROTECTION_CLAUSE}", (index_id,)
                ).rowcount
                chunks = connection.execute(
                    f"DELETE FROM semantic_chunks WHERE index_id=? AND {_PROTECTION_CLAUSE}", (index_id,)
                ).rowcount
                documents = connection.execute(
                    f"DELETE FROM semantic_documents WHERE index_id=? AND {_PROTECTION_CLAUSE}", (index_id,)
                ).rowcount
                registry = connection.execute(
                    f"DELETE FROM semantic_indices WHERE index_id=? AND {_PROTECTION_CLAUSE} "
                    "AND status NOT IN (?, ?)",
                    (index_id, *PROTECTED_STATUSES),
                ).rowcount
                if not registry:
                    skipped.append(index_id)
                    continue
                pruned.append(index_id)
                counts["indices"] += int(registry)
                counts["documents"] += int(documents)
                counts["chunks"] += int(chunks)
                counts["fts"] += int(fts)
        # Keep the WAL bounded after bulk deletes; the passive checkpoint runs
        # after commit and never blocks concurrent writers for long.
        with suppress(sqlite3.Error):
            connection.execute("PRAGMA wal_checkpoint")
    return {"pruned_index_ids": pruned, "skipped_index_ids": skipped, "counts": counts}


def _rotate_anchors(anchor_dir: Path, db_stem: str, keep: int) -> tuple[list[str], list[str]]:
    """Keep the newest ``keep`` prune anchors; remove older ones (and sidecars)."""
    prefix = f"{db_stem}.index-prune-anchor."
    try:
        candidates = sorted(
            (path for path in anchor_dir.glob(f"{prefix}*.sqlite") if path.is_file()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
    except OSError:
        return [], []
    removed: list[str] = []
    for stale in candidates[:-keep] if keep else candidates:
        with suppress(OSError):
            remove_sqlite_artifacts(stale)
        removed.append(stale.name)
    return removed, [path.name for path in (candidates[-keep:] if keep else [])]


def _newest_anchor_age_seconds(anchor_dir: Path, db_stem: str) -> float | None:
    """Age in seconds of the newest prune anchor, or None when none exists."""
    prefix = f"{db_stem}.index-prune-anchor."
    try:
        candidates = [path for path in anchor_dir.glob(f"{prefix}*.sqlite") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        newest_mtime = max(path.stat().st_mtime for path in candidates)
    except OSError:
        return None
    return max(0.0, time.time() - newest_mtime)


def _verify_after_prune(active_index_id: str, expected_chunk_count: int) -> dict[str, Any]:
    """Tripwire checks: active generation untouched and FTS references intact."""
    with closing(connect()) as connection, connection:
        active_chunks = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_chunks WHERE index_id=?", (active_index_id,)
            ).fetchone()[0]
        )
        registry_active = connection.execute(
            "SELECT COUNT(*) FROM semantic_indices WHERE index_id=?", (active_index_id,)
        ).fetchone()[0]
        orphan_fts = int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_chunks_fts f WHERE NOT EXISTS ("
                "SELECT 1 FROM semantic_chunks c WHERE c.index_id=f.index_id AND c.chunk_id=f.chunk_id)"
            ).fetchone()[0]
        )
    alerts: list[str] = []
    if active_chunks != expected_chunk_count:
        alerts.append("active_chunk_count_changed")
    if not registry_active:
        alerts.append("active_registry_row_missing")
    if orphan_fts:
        alerts.append("fts_orphan_rows")
    return {
        "active_chunk_count_after": active_chunks,
        "fts_orphan_count": orphan_fts,
        "alert": ",".join(alerts),
    }


def _append_prune_log(anchor_dir: Path, report: dict[str, Any]) -> None:
    """Append one structured JSONL line per policy invocation (best effort)."""
    with suppress(OSError):
        anchor_dir.mkdir(parents=True, exist_ok=True)
        secure_private_directory(anchor_dir)
        with anchor_dir.joinpath(PRUNE_LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")

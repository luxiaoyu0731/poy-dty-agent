"""Use-case orchestration for the intelligence pipeline (projection ->
cutoff manifest -> clustering/analysis -> brief materialize).

Discipline (spec 15.4): cross-process single-flight file locks (``flock`` is
released by the OS on crash), short ``BEGIN IMMEDIATE`` transactions, no
network/LLM work inside write locks, terminal ``intelligence_runs`` rows
appended exactly once per attempt, and atomic checkpoint files bound to
``run_id``/provider/cursor/input hash/generation.
"""

from __future__ import annotations

import base64
import binascii as binascii_error
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from . import analysis, brief, clustering, identity, metrics, projection, providers
from . import storage as domain_storage
from .identity import canonical_json, sha256_hex

RUNS_DIR_ENV = "INTELLIGENCE_RUN_DIR"
DEFAULT_RUNS_DIR = "data/intelligence-runs"
PIPELINE_PAGE_BUDGET_SECONDS = 30.0
CLUSTER_BATCH_LIMIT = 200


class IntelligenceRunError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run_root() -> Path:
    configured = os.getenv(RUNS_DIR_ENV)
    if configured:
        return Path(configured)
    from ..settings import settings

    base = Path(settings.sqlite_path)
    if not base.is_absolute():
        server_root = Path(__file__).resolve().parents[2]
        base = server_root / base
    return base.parent / "intelligence-runs"


@contextmanager
def single_flight(key: str):
    """Cross-process lock; the OS releases ``flock`` when a process dies."""

    root = run_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / f"{key}.lock"
    handle = open(lock_path, "a+")  # noqa: SIM115 - flock handle lives for the context duration
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IntelligenceRunError(
                "intelligence_run_in_progress",
                f"another run holds the single-flight lock for {key}",
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


SEARCH_REBUILD_FLAG = "search-rebuilding.flag"


def rebuild_search_with_block(connection) -> int:
    """Mark search unavailable, rebuild the derived FTS, then release.

    The flag file lives in the run root; the search route fails closed with
    ``intelligence_search_unavailable`` while it exists. Business tables are
    never modified.
    """

    root = run_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    flag_path = root / SEARCH_REBUILD_FLAG
    handle = open(flag_path, "w")  # noqa: SIM115 - flag lives for the rebuild duration
    try:
        os.chmod(flag_path, 0o600)
        handle.write(identity.utc_now_iso())
        handle.flush()
        os.fsync(handle.fileno())
        with domain_storage.short_write_transaction(connection):
            return domain_storage.rebuild_search_index(connection)
    finally:
        handle.close()
        with suppress(FileNotFoundError):
            os.unlink(flag_path)


def search_rebuilding() -> bool:
    return (run_root() / SEARCH_REBUILD_FLAG).exists()


def new_run_id() -> str:
    return str(uuid.uuid4())


def write_checkpoint(
    *, run_id: str, stage: str, provider: str | None, cursor: int, generation: int, input_sha256: str
) -> Path:
    root = run_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "checkpoint_version": "intelligence-checkpoint.v1",
        "run_id": run_id,
        "stage": stage,
        "provider": provider,
        "cursor": cursor,
        "generation": generation,
        "input_sha256": input_sha256,
        "written_at": identity.utc_now_iso(),
    }
    final_path = root / f"checkpoint-{stage}-{sha256_hex(f'{stage}:{provider}')[:12]}.json"
    handle_fd, temp_name = tempfile.mkstemp(dir=root, prefix=".checkpoint-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w") as stream:
            os.chmod(temp_name, 0o600)
            stream.write(canonical_json(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, final_path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    return final_path


def append_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    run_type: str,
    provider_id: str | None,
    business_date: str | None,
    started_at: str,
    status: str,
    counts: dict[str, int],
    cursor_before: int | None = None,
    cursor_after: int | None = None,
    degraded_reasons: list[str] | None = None,
    error_code: str | None = None,
    error_detail_safe: str | None = None,
    input_sha256: str | None = None,
    output_sha256: str | None = None,
    cutoff_input_manifest_sha256: str | None = None,
    parent_run_ids: list[str] | None = None,
) -> str:
    finished_at = identity.utc_now_iso()
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    record = {
        "schema_version": identity.SCHEMA_VERSION,
        "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
        "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
        "run_id": run_id,
        "run_type": run_type,
        "provider_id": provider_id,
        "business_date": business_date,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "duration_ms": max(0, int((finished - started).total_seconds() * 1000)),
        "cursor_before": {"news_rowid": cursor_before} if cursor_before is not None else None,
        "cursor_after": ({"news_rowid": cursor_after,
                          "projection_version": projection.PROJECTION_SCHEMA_VERSION}
                         if cursor_after is not None and run_type == "projection"
                         else {"news_rowid": cursor_after} if cursor_after is not None else None),
        "parent_run_ids": list(parent_run_ids or []),
        "cutoff_input_manifest_sha256": cutoff_input_manifest_sha256,
        "counts": counts,
        "degraded_reasons": list(degraded_reasons or []),
        "error_code": error_code,
        "error_detail_safe": error_detail_safe,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "created_at": finished_at,
    }
    with domain_storage.short_write_transaction(connection):
        stored_run_id, inserted = domain_storage.insert_run(connection, record)
    if inserted:
        metrics.observe_run(
            run_type=run_type,
            provider=provider_id,
            status=status,
            duration_seconds=max(0.0, (finished - started).total_seconds()),
        )
    return stored_run_id


@dataclass
class PipelineResult:
    provider_run_ids: list[str]
    failed_provider_ids: list[str]
    projection_run_id: str
    clustering_run_id: str
    brief_id: str
    brief_status: str
    brief_replayed: bool
    inserted_items: int
    new_events: int


@dataclass(frozen=True)
class ProviderStageResult:
    run_id: str
    provider_id: str
    status: str
    inserted: int
    existing: int
    rejected: int
    degraded_reasons: list[str]
    error_code: str | None


def run_usgs_provider_stage(
    connection: sqlite3.Connection,
    *,
    business_date: str | None,
    client: httpx.Client | None = None,
) -> ProviderStageResult:
    """Fetch and project USGS outside the write lock; always append a terminal run."""

    run_id = new_run_id()
    started_at = identity.utc_now_iso()
    owned_client = client is None
    active_client = client or httpx.Client()
    try:
        try:
            outcome = providers.fetch_usgs_week_feed(client=active_client)
        except Exception as exc:  # noqa: BLE001 - provider attempts must terminate honestly
            outcome = providers.ProviderOutcome(
                provider_id=providers.USGS_PROVIDER_ID,
                status="failed",
                degraded_reasons=["unexpected_provider_failure"],
                error_code="usgs_provider_failed",
            )
            error_detail_safe = type(exc).__name__
        else:
            error_detail_safe = None
    finally:
        if owned_client:
            active_client.close()

    counts = {
        "input": outcome.input_count,
        "inserted": 0,
        "existing": 0,
        "revised": 0,
        "rejected": outcome.rejected_count + outcome.truncated_count,
    }
    status = outcome.status
    error_code = outcome.error_code
    degraded_reasons = list(outcome.degraded_reasons)
    if status != "failed":
        try:
            with domain_storage.short_write_transaction(connection):
                counts["inserted"], counts["existing"] = providers.project_usgs_outcome(
                    connection, outcome
                )
        except Exception as exc:  # noqa: BLE001 - isolate this non-critical provider
            status = "failed"
            error_code = "usgs_projection_failed"
            error_detail_safe = type(exc).__name__
            degraded_reasons.append("provider_projection_failed")

    append_run(
        connection,
        run_id=run_id,
        run_type="provider",
        provider_id=providers.USGS_PROVIDER_ID,
        business_date=business_date,
        started_at=started_at,
        status=status,
        counts=counts,
        degraded_reasons=degraded_reasons,
        error_code=error_code,
        error_detail_safe=error_detail_safe,
        input_sha256=outcome.input_sha256,
        output_sha256=outcome.output_sha256,
    )
    metrics.observe_projection_items(
        provider=providers.USGS_PROVIDER_ID, result="inserted", count=counts["inserted"]
    )
    metrics.observe_projection_items(
        provider=providers.USGS_PROVIDER_ID, result="existing", count=counts["existing"]
    )
    metrics.observe_projection_items(
        provider=providers.USGS_PROVIDER_ID, result="rejected", count=counts["rejected"]
    )
    return ProviderStageResult(
        run_id=run_id,
        provider_id=providers.USGS_PROVIDER_ID,
        status=status,
        inserted=counts["inserted"],
        existing=counts["existing"],
        rejected=counts["rejected"],
        degraded_reasons=degraded_reasons,
        error_code=error_code,
    )


def run_news_projection_stage(
    connection: sqlite3.Connection,
    *,
    business_date: str | None,
    deadline_seconds: float = PIPELINE_PAGE_BUDGET_SECONDS,
    max_items: int | None = None,
) -> str:
    """Project one bounded page batch of legacy news; resumable by cursor."""

    run_id = new_run_id()
    started_at = identity.utc_now_iso()
    cursor_row = connection.execute(
        """
        SELECT cursor_after_json FROM intelligence_runs
        WHERE run_type='projection' AND status IN ('succeeded','degraded')
        ORDER BY append_seq DESC LIMIT 1
        """
    ).fetchone()
    cursor = 0
    if cursor_row is not None and cursor_row["cursor_after_json"]:
        saved = json.loads(str(cursor_row["cursor_after_json"]))
        # A new parser/mapping must revisit old rows, appending revisions rather
        # than leaving them permanently behind an insertion-only cursor.
        if saved.get("projection_version") == projection.PROJECTION_SCHEMA_VERSION:
            cursor = int(saved.get("news_rowid", 0))
    initial_cursor = cursor
    counts = {"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0}
    degraded: list[str] = []
    deadline = time.monotonic() + deadline_seconds
    input_hash = sha256_hex(f"news_articles:{cursor}")
    try:
        while time.monotonic() < deadline:
            if max_items is not None and counts["input"] >= max_items:
                break
            page_limit = projection.PROJECTION_PAGE_SIZE
            if max_items is not None:
                page_limit = min(page_limit, max_items - counts["input"])
            with domain_storage.short_write_transaction(connection):
                outcome = projection.run_news_projection(
                    connection,
                    cursor=cursor,
                    limit=page_limit,
                )
            counts["input"] += outcome.scanned
            counts["inserted"] += outcome.inserted
            counts["existing"] += outcome.existing
            counts["rejected"] += outcome.rejected
            write_checkpoint(
                run_id=run_id,
                stage="projection",
                provider="legacy_news_articles",
                cursor=outcome.last_rowid,
                generation=counts["input"],
                input_sha256=input_hash,
            )
            if outcome.scanned == 0:
                break
            cursor = outcome.last_rowid
        else:
            degraded.append("projection_deadline_exceeded_resumable")
    except Exception as exc:  # noqa: BLE001 - terminal audit row must record the failure
        append_run(
            connection,
            run_id=run_id,
            run_type="projection",
            provider_id="legacy_news_articles",
            business_date=business_date,
            started_at=started_at,
            status="failed",
            counts=counts,
            cursor_before=initial_cursor,
            cursor_after=cursor,
            degraded_reasons=degraded,
            error_code=getattr(exc, "code", "intelligence_projection_failed"),
            error_detail_safe=(
                f"{getattr(exc, 'sqlite_errorname', 'SQLITE_CONSTRAINT')}: {str(exc)[:200]}"
                if isinstance(exc, sqlite3.IntegrityError) else type(exc).__name__
            ),
            input_sha256=input_hash,
        )
        raise
    status = "degraded" if degraded else "succeeded"
    append_run(
        connection,
        run_id=run_id,
        run_type="projection",
        provider_id="legacy_news_articles",
        business_date=business_date,
        started_at=started_at,
        status=status,
        counts=counts,
        cursor_before=initial_cursor,
        cursor_after=cursor,
        degraded_reasons=degraded,
        input_sha256=input_hash,
    )
    metrics.observe_projection_items(
        provider="legacy_news_articles", result="inserted", count=counts["inserted"]
    )
    metrics.observe_projection_items(
        provider="legacy_news_articles", result="existing", count=counts["existing"]
    )
    metrics.observe_projection_items(
        provider="legacy_news_articles", result="rejected", count=counts["rejected"]
    )
    return run_id


def append_analyzed_cluster(connection, cluster, *, as_of_time, supersedes_revision_id=None,
                            split_from_event_id=None):
    """Append analysis and its evidence atomically in a caller-owned transaction."""
    if not connection.in_transaction:
        raise IntelligenceRunError("intelligence_transaction_required", "cluster append needs a transaction")
    members = clustering.cluster_members_ordered(cluster)
    member_roles = [clustering.evidence_role_for_member(m, cluster) for m in members]
    event_record = analysis.analyze_cluster(cluster, as_of_time=as_of_time)
    # An updated source item is new evidence, not a new event origin. Retain
    # the immutable first anchor while linking this analysis to current item
    # revisions below. Never relax the database's append-only guard.
    original_anchor = connection.execute(
        "SELECT anchor_item_id, anchor_item_revision_id, first_seen_at "
        "FROM intelligence_event_revisions WHERE event_id=? AND revision_no=1",
        (cluster.event_id,),
    ).fetchone()
    if original_anchor is not None:
        event_record.update({key: original_anchor[key] for key in (
            "anchor_item_id", "anchor_item_revision_id", "first_seen_at"
        )})
    if supersedes_revision_id:
        event_record["supersedes_revision_id"] = supersedes_revision_id
    if split_from_event_id:
        event_record["split_from_event_id"] = split_from_event_id
    prepared = domain_storage.prepare_event_revision(connection, event_record)
    revision_id = str(prepared["event_revision_id"])
    link_ids_by_claim: dict[str, str] = {}
    for member, (role, _) in zip(members, member_roles, strict=True):
        claim_id = (
            analysis.fact_claim_id(cluster.event_id, member.item_revision_id)
            if role == "fact"
            else identity.stable_uuid(
                "claim", cluster.event_id, member.item_revision_id, role
            )
        )
        link_ids_by_claim[claim_id] = identity.evidence_link_id_for(
            event_revision_id=revision_id,
            item_revision_id=member.item_revision_id,
            claim_id=claim_id,
            evidence_role=role,
        )
    for fact in prepared.get("facts") or []:
        fact["evidence_link_ids"] = [
            link_ids_by_claim[str(fact.get("claim_id"))]
        ] if str(fact.get("claim_id")) in link_ids_by_claim else []
    event_revision_id2, _, inserted = domain_storage.insert_prepared_event_revision(
        connection, prepared
    )
    revision_id = event_revision_id2
    existing = int(not inserted)
    for member, (role, independent) in zip(members, member_roles, strict=True):
        link_id = identity.evidence_link_id_for(
            event_revision_id=revision_id,
            item_revision_id=member.item_revision_id,
            claim_id=analysis.fact_claim_id(cluster.event_id, member.item_revision_id)
            if role == "fact"
            else identity.stable_uuid(
                "claim", cluster.event_id, member.item_revision_id, role
            ),
            evidence_role=role,
        )
        _, link_inserted = domain_storage.insert_evidence_link(
            connection,
            {
                "schema_version": identity.SCHEMA_VERSION,
                "identity_policy_version": identity.INTELLIGENCE_IDENTITY_POLICY_VERSION,
                "payload_manifest_version": identity.PAYLOAD_MANIFEST_VERSION,
                "evidence_link_id": link_id,
                "event_revision_id": revision_id,
                "item_revision_id": member.item_revision_id,
                "claim_id": analysis.fact_claim_id(cluster.event_id, member.item_revision_id)
                if role == "fact"
                else identity.stable_uuid(
                    "claim", cluster.event_id, member.item_revision_id, role
                ),
                "evidence_role": role,
                "origin_group_id": member.origin_group_id,
                "independent_corroboration": independent,
                "citation_label": member.collector_source_id,
                "created_at": as_of_time,
            },
        )
        if not link_inserted:
            existing += 1
    return int(inserted), existing


def run_clustering_analysis_stage(
    connection: sqlite3.Connection,
    *,
    business_date: str,
    cutoff_at: str | None = None,
    parent_run_ids: list[str] | None = None,
    item_high_water: int | None = None,
    cutoff_manifest_sha256: str | None = None,
) -> tuple[str, int]:
    """Cluster unclustered items and append event revisions + evidence edges."""

    run_id = new_run_id()
    started_at = identity.utc_now_iso()
    cutoff = cutoff_at or brief.cutoff_at_for(business_date)
    counts = {"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 0}
    new_events = 0
    try:
        if item_high_water is None:
            head_row = connection.execute(
                "SELECT COALESCE(MAX(append_seq), 0) AS hw "
                "FROM intelligence_item_revisions WHERE created_at <= ?",
                (cutoff,),
            ).fetchone()
            max_append_seq = int(head_row["hw"])
        else:
            max_append_seq = max(0, int(item_high_water))
        cursor = 0
        while True:
            rows = clustering.collect_cluster_batch(
                connection, max_append_seq=max_append_seq, cursor=cursor, limit=CLUSTER_BATCH_LIMIT
            )
            if not rows:
                break
            counts["input"] += len(rows)
            for cluster in clustering.build_clusters(rows):
                with domain_storage.short_write_transaction(connection):
                    inserted, existing = append_analyzed_cluster(
                        connection, cluster, as_of_time=started_at
                    )
                    new_events += inserted
                    counts["inserted"] += inserted
                    counts["existing"] += existing
            cursor = int(rows[-1]["append_seq"])
            if len(rows) < CLUSTER_BATCH_LIMIT:
                break
    except Exception as exc:  # noqa: BLE001
        append_run(
            connection,
            run_id=run_id,
            run_type="clustering",
            provider_id=None,
            business_date=business_date,
            started_at=started_at,
            status="failed",
            counts=counts,
            degraded_reasons=[],
            error_code="intelligence_clustering_failed",
            error_detail_safe=(
                f"{getattr(exc, 'sqlite_errorname', 'SQLITE_CONSTRAINT')}: {str(exc)[:200]}"
                if isinstance(exc, sqlite3.IntegrityError) else type(exc).__name__
            ),
            parent_run_ids=parent_run_ids,
            cutoff_input_manifest_sha256=cutoff_manifest_sha256,
        )
        raise
    append_run(
        connection,
        run_id=run_id,
        run_type="clustering",
        provider_id=None,
        business_date=business_date,
        started_at=started_at,
        status="succeeded",
        counts=counts,
        parent_run_ids=parent_run_ids,
        cutoff_input_manifest_sha256=cutoff_manifest_sha256,
    )
    metrics.observe_events(result="inserted", count=new_events)
    return run_id, new_events


def collect_daily_inputs(
    connection: sqlite3.Connection,
    *,
    business_date: str,
    include_usgs: bool = True,
    projection_deadline_seconds: float = PIPELINE_PAGE_BUDGET_SECONDS,
) -> dict[str, object]:
    """Collect before cutoff without prematurely freezing a daily brief."""
    with single_flight(f"daily:{business_date}"):
        provider_results = []
        if include_usgs:
            with single_flight(f"provider:{providers.USGS_PROVIDER_ID}"):
                provider_results.append(run_usgs_provider_stage(connection, business_date=business_date))
        with single_flight("projection:legacy_news_articles"):
            projection_run_id = run_news_projection_stage(
                connection, business_date=business_date, deadline_seconds=projection_deadline_seconds
            )
        projection_run = connection.execute(
            "SELECT status FROM intelligence_runs WHERE run_id=?", (projection_run_id,)
        ).fetchone()
        projection_complete = projection_run is not None and projection_run["status"] == "succeeded"
        return {
            "status": "blocked" if not projection_complete else (
                "ready_with_gaps" if any(row.status != "succeeded" for row in provider_results) else "ready"
            ),
            "blockers": [] if projection_complete else ["news projection needs a bounded retry before cutoff"],
            "provider_run_ids": [row.run_id for row in provider_results],
            "projection_run_id": projection_run_id,
            "brief_created": False,
        }


def run_daily_pipeline(
    connection: sqlite3.Connection,
    *,
    business_date: str,
    projection_deadline_seconds: float = PIPELINE_PAGE_BUDGET_SECONDS,
    include_usgs: bool = True,
    usgs_client: httpx.Client | None = None,
) -> PipelineResult:
    """Run one business-date DAG under a cross-process top-level lock."""

    with single_flight(f"daily:{business_date}"):
        return _run_daily_pipeline_unlocked(
            connection,
            business_date=business_date,
            projection_deadline_seconds=projection_deadline_seconds,
            include_usgs=include_usgs,
            usgs_client=usgs_client,
        )


def _run_daily_pipeline_unlocked(
    connection: sqlite3.Connection,
    *,
    business_date: str,
    projection_deadline_seconds: float,
    include_usgs: bool,
    usgs_client: httpx.Client | None,
) -> PipelineResult:
    """Provider/projection -> clustering/analysis -> brief stage implementation."""

    provider_results: list[ProviderStageResult] = []
    if include_usgs:
        with single_flight(f"provider:{providers.USGS_PROVIDER_ID}"):
            provider_results.append(
                run_usgs_provider_stage(
                    connection,
                    business_date=business_date,
                    client=usgs_client,
                )
            )
    projection_key = "projection:legacy_news_articles"
    cluster_key = f"clustering:{business_date}"
    brief_key = f"brief:{business_date}"
    with single_flight(projection_key):
        projection_run_id = run_news_projection_stage(
            connection, business_date=business_date, deadline_seconds=projection_deadline_seconds
        )
    upstream_run_ids = [result.run_id for result in provider_results] + [projection_run_id]
    cutoff_manifest, cutoff_manifest_sha256 = brief.build_cutoff_input_manifest(
        connection,
        business_date=business_date,
        cutoff_at=brief.cutoff_at_for(business_date),
    )
    item_high_water = int(
        cutoff_manifest["high_water_append_seq"]["intelligence_item_revisions"]  # type: ignore[index]
    )
    with single_flight(cluster_key):
        clustering_run_id, new_events = run_clustering_analysis_stage(
            connection,
            business_date=business_date,
            parent_run_ids=upstream_run_ids,
            item_high_water=item_high_water,
            cutoff_manifest_sha256=cutoff_manifest_sha256,
        )
    source_run_ids = [*upstream_run_ids, clustering_run_id]
    provider_gaps = [
        {
            "code": "noncritical_provider_failed"
            if result.status == "failed"
            else "noncritical_provider_degraded",
            "scope": result.provider_id,
            "message_safe": f"{result.provider_id} ended with status {result.status}",
        }
        for result in provider_results
        if result.status != "succeeded"
    ]
    with single_flight(brief_key):
        brief_run_id = new_run_id()
        brief_started = identity.utc_now_iso()
        try:
            materialization = brief.materialize_daily_brief(
                connection,
                business_date=business_date,
                now=brief_started,
                parent_run_ids=source_run_ids,
                additional_gaps=provider_gaps,
                cutoff_input_manifest=cutoff_manifest,
                cutoff_input_manifest_sha256=cutoff_manifest_sha256,
            )
        except Exception as exc:
            append_run(
                connection,
                run_id=brief_run_id,
                run_type="brief",
                provider_id=None,
                business_date=business_date,
                started_at=brief_started,
                status="failed",
                counts={"input": 0, "inserted": 0, "existing": 0, "revised": 0, "rejected": 1},
                error_code=getattr(exc, "code", "intelligence_brief_failed"),
                error_detail_safe=type(exc).__name__,
                parent_run_ids=source_run_ids,
                cutoff_input_manifest_sha256=cutoff_manifest_sha256,
            )
            raise
        brief_status = (
            "failed"
            if materialization.status == "blocked"
            else "degraded"
            if materialization.status == "ready_with_gaps"
            else "succeeded"
        )
        append_run(
            connection,
            run_id=brief_run_id,
            run_type="brief",
            provider_id=None,
            business_date=business_date,
            started_at=brief_started,
            status=brief_status,
            counts={
                "input": len(materialization.selected_event_revision_ids),
                "inserted": 1 if not materialization.replayed else 0,
                "existing": 0 if not materialization.replayed else 1,
                "revised": 0,
                "rejected": 0,
            },
            error_code="intelligence_brief_blocked"
            if materialization.status == "blocked"
            else None,
            parent_run_ids=source_run_ids,
            cutoff_input_manifest_sha256=cutoff_manifest_sha256,
        )
        metrics.observe_brief(
            status=materialization.status,
            event_count=len(materialization.selected_event_revision_ids),
        )
    return PipelineResult(
        provider_run_ids=[result.run_id for result in provider_results],
        failed_provider_ids=[
            result.provider_id for result in provider_results if result.status == "failed"
        ],
        projection_run_id=projection_run_id,
        clustering_run_id=clustering_run_id,
        brief_id=materialization.brief_id,
        brief_status=materialization.status,
        brief_replayed=materialization.replayed,
        inserted_items=sum(result.inserted for result in provider_results)
        + _run_count(connection, projection_run_id, "inserted_count"),
        new_events=new_events,
    )


def _run_count(connection: sqlite3.Connection, run_id: str, column: str) -> int:
    if column not in {"input_count", "inserted_count", "existing_count", "rejected_count"}:
        raise ValueError("unsupported run count")
    row = connection.execute(
        f"SELECT {column} AS value FROM intelligence_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["value"] if row is not None else 0)

# ---------------------------------------------------------------------------
# Snapshot pagination cursors: HMAC-signed, bound to the frozen high-water
# manifest, filter hash, sort key, and contract version (spec 11.10 / 12.1).
# ---------------------------------------------------------------------------

CURSOR_CONTRACT_VERSION = "intelligence-cursor.v1"
_CURSOR_SECRET_CACHE: bytes | None = None


def _cursor_secret() -> bytes:
    global _CURSOR_SECRET_CACHE
    if _CURSOR_SECRET_CACHE is not None:
        return _CURSOR_SECRET_CACHE
    from ..settings import settings

    if settings.intelligence_cursor_secret:
        material = settings.intelligence_cursor_secret
    elif settings.internal_api_token:
        material = settings.internal_api_token
    else:
        material = uuid.uuid4().hex  # process-local fallback for dev/test
    _CURSOR_SECRET_CACHE = hashlib.sha256(
        ("intelligence-cursor-v1:" + material).encode("utf-8")
    ).digest()
    return _CURSOR_SECRET_CACHE


def reset_cursor_secret_cache() -> None:
    global _CURSOR_SECRET_CACHE
    _CURSOR_SECRET_CACHE = None


def _sign(payload: bytes | str) -> str:
    import hmac as hmac_module

    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hmac_module.new(_cursor_secret(), payload, hashlib.sha256).hexdigest()


def build_snapshot_id(manifest: dict[str, int], snapshot_at: str) -> str:
    return "snap-" + _sign(canonical_json({"high_water": manifest, "snapshot_at": snapshot_at}))[:32]


def encode_cursor(
    *,
    manifest: dict[str, int],
    snapshot_at: str,
    filters_hash: str,
    sort_key: str,
    offset: int,
) -> str:
    body = canonical_json(
        {
            "contract_version": CURSOR_CONTRACT_VERSION,
            "high_water": manifest,
            "snapshot_at": snapshot_at,
            "filters_hash": filters_hash,
            "sort_key": sort_key,
            "offset": offset,
        }
    )
    payload = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return payload + "." + _sign(body.encode("utf-8"))


def decode_cursor(token: str) -> dict[str, object]:
    import hmac as hmac_module

    try:
        payload_b64, signature = token.split(".", 1)
        body = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii_error) as exc:
        raise IntelligenceRunError("intelligence_cursor_invalid", "cursor is malformed") from exc
    if not hmac_module.compare_digest(_sign(body), signature):
        raise IntelligenceRunError("intelligence_cursor_invalid", "cursor signature mismatch")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntelligenceRunError("intelligence_cursor_invalid", "cursor body unreadable") from exc
    if not isinstance(decoded, dict) or decoded.get("contract_version") != CURSOR_CONTRACT_VERSION:
        raise IntelligenceRunError("intelligence_cursor_invalid", "cursor contract mismatch")
    high_water = decoded.get("high_water")
    if not isinstance(high_water, dict) or not high_water:
        raise IntelligenceRunError("intelligence_cursor_invalid", "cursor manifest missing")
    return decoded


def hash_filters(filters: dict[str, object]) -> str:
    return sha256_hex(canonical_json(filters))

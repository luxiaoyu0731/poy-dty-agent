"""Read-only projections of frozen issued evidence or a bounded current preview."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .evidence_semantic_review import SemanticReview, project_reviews
from .prediction_main import MainInputSnapshot, capture_main_inputs
from .settings import settings
from .seven_product_forecast_ledger import get_latest_issued_seven_product_forecast
from .single_flight import SingleFlight

Target = Literal["crude", "naphtha", "px", "pta", "meg", "poy", "dty"]
View = Literal["issued", "current"]
# Guards the in-memory dicts below only (microseconds); builds run outside it so
# cache hits and unrelated keys never queue behind a capture.
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, MainInputSnapshot]] = {}
_clock = time.monotonic
CURRENT_TTL_SECONDS = 60
# Expired current snapshots stay as an honest fallback for stale-while-revalidate
# before retention removes them; as_of_time always states the real cutoff.
CURRENT_RETENTION_SECONDS = 900
PIN_TTL_SECONDS = 600
MAX_PINNED_SNAPSHOTS = 3
# Cold capture can take tens of seconds on the production database. Joins share
# the in-flight build; the wait plus the build stay inside the frontend request
# deadline because the capture itself carries internal deadlines.
CAPTURE_JOIN_TIMEOUT_SECONDS = 55
_CURRENT_BUILDS: SingleFlight[MainInputSnapshot] = SingleFlight()
_ISSUED_BUILDS: SingleFlight[MainInputSnapshot] = SingleFlight()
# Debounce state for the stale-while-revalidate background rebuild.
_REBUILD_GUARD = threading.Lock()
_REBUILD_RUNNING = threading.Event()


def _prune_cache() -> None:
    with _CACHE_LOCK:
        for key in list(_CACHE):
            ttl = CURRENT_RETENTION_SECONDS if key.endswith(":current") else PIN_TTL_SECONDS
            if _clock() - _CACHE[key][0] >= ttl:
                del _CACHE[key]
        pinned = sorted(
            (key for key in _CACHE if not key.endswith(":current")),
            key=lambda key: _CACHE[key][0],
            reverse=True,
        )
        for key in pinned[MAX_PINNED_SNAPSHOTS:]:
            del _CACHE[key]


class DossierCoverage(BaseModel):
    mechanism: str
    label: str
    automatic_scope: str
    status: Literal["available", "needs_review", "no_material"]
    stored_claims: int
    source_claims: int
    usable_episodes: int


class DossierClaim(BaseModel):
    claim_id: str
    target: Target
    mechanism: str
    subject: str
    event_date: str | None
    event_date_source: str | None = None
    state: Literal["actual", "planned", "unconfirmed", "denied", "in_progress", "unknown"]
    semantic_status: Literal["rule_checked", "needs_review"]
    expected_direction: Literal["up", "down"] | None
    quote: str = Field(max_length=1600)
    source_url: str
    source_title: str
    source_tier: str
    published_at: str
    known_at: str
    gaps: list[str]
    inference_boundary: str


class DossierHistorical(BaseModel):
    claim_id: str
    relation: Literal["support", "counter", "neutral", "unresolved"]
    outcome: dict[str, object]
    use: Literal["retrospective_context_not_historical_forecast_input"]
    causality_proven: Literal[False]


class DossierEventPath(BaseModel):
    target: Target
    label: str


class DossierEventChain(BaseModel):
    proof_kind: Literal["rule", "semantic_review", "unreviewed_material"] = "unreviewed_material"
    semantic_review: SemanticReview | None = None
    chain_id: str
    claim_id: str
    source_target: Target
    target: Target
    relation: Literal["direct", "upstream_context"]
    mechanism: str
    event_label: str
    quote: str = Field(max_length=1600)
    source_url: str
    source_title: str
    event_date: str | None
    event_date_source: str | None = None
    published_at: str
    known_at: str
    state: Literal["actual", "planned", "unconfirmed", "denied", "in_progress", "unknown"]
    semantic_status: Literal["rule_checked", "needs_review"]
    conditions: list[str]
    path: list[DossierEventPath]
    counts_as_evidence: bool
    direction: Literal["up", "down"] | None


class EvidenceDossierResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["product", "event", "report", "answer", "batch"] = "product"
    context_id: str | None = None
    context_revision: str | None = None
    scope_note: str = "品种整体分析资料，不自动证明具体事件或回答"
    matched_source_claims: int = 0
    schema_version: Literal["business-evidence-view.v1"] = "business-evidence-view.v1"
    view: View
    target: Target
    horizon_days: Literal[1, 7, 30]
    status: Literal["available_with_gaps", "capture_failed", "legacy_input", "unavailable"]
    # True when this response serves the previous snapshot while one rebuild for
    # the newest data runs in the background. The cutoff stays honest in
    # as_of_time; callers surface the state instead of presenting it as fresh.
    revalidating: bool = False
    batch_id: str | None = None
    as_of_time: str | None = None
    input_sha256: str | None = None
    hypothesis: str = ""
    model_effect: Literal["context_only"] = "context_only"
    market_baseline: dict[str, object] = Field(default_factory=dict)
    source_rows: int = 0
    capture_complete: bool = False
    coverage: list[DossierCoverage] = Field(default_factory=list)
    claims: list[DossierClaim] = Field(default_factory=list)
    event_chains: list[DossierEventChain] = Field(default_factory=list)
    semantic_reviews: list[SemanticReview] = Field(default_factory=list)
    current_support: list[str] = Field(default_factory=list)
    current_counter: list[str] = Field(default_factory=list)
    historical_support: list[DossierHistorical] = Field(default_factory=list)
    historical_counter: list[DossierHistorical] = Field(default_factory=list)
    historical_other: list[DossierHistorical] = Field(default_factory=list)
    other_materials: list[str] = Field(default_factory=list)
    mixed: list[list[str]] = Field(default_factory=list)
    current_support_episodes: int = 0
    current_counter_episodes: int = 0
    gaps: list[str] = Field(default_factory=list)
    source_gaps: dict[str, int] = Field(default_factory=dict)
    total_claims: int = 0
    offset: int = 0
    next_offset: int | None = None


def _load_issued_archive(sha: str, as_of_time: str) -> MainInputSnapshot:
    path = Path(settings.sqlite_path).resolve().parent / "prediction-inputs" / f"{sha}.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > 96 * 1024**2:
            raise ValueError("issued_input_archive_bounds")
        body = json.load(stream)
    if body.get("content_sha256") != sha or body.get("as_of_time") != as_of_time:
        raise ValueError("issued_input_archive_binding_mismatch")
    return MainInputSnapshot(body)


def _issued_snapshot(batch_id: str | None = None):
    if batch_id:
        from .seven_product_forecast_ledger import get_seven_product_forecast_batch

        ledger = get_seven_product_forecast_batch(batch_id=batch_id)
        if ledger is None:
            raise LookupError("forecast_batch_not_found")
        from types import SimpleNamespace

        batch = SimpleNamespace(
            batch_id=ledger.batch_id, as_of_time=ledger.as_of_time, cells=[cell.forecast for cell in ledger.cells]
        )
    else:
        batch = get_latest_issued_seven_product_forecast()
    if batch is None:
        return None, None
    hashes = {c.input_snapshot_sha256 for c in batch.cells}
    if len(hashes) != 1:
        raise ValueError("issued_input_binding_inconsistent")
    sha = next(iter(hashes))
    if sha is None:
        return batch, None
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("issued_input_identity_invalid")
    cache_key = f"{settings.sqlite_path}:{sha}"
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return batch, cached[1]
    # First reader parses the archive; concurrent first readers for the same
    # identity share one parse instead of each loading tens of megabytes.
    snapshot, _ = _ISSUED_BUILDS.run(
        cache_key,
        lambda: _load_issued_archive(sha, batch.as_of_time),
        join_timeout=CAPTURE_JOIN_TIMEOUT_SECONDS,
    )
    with _CACHE_LOCK:
        _CACHE[cache_key] = (_clock(), snapshot)
    return batch, snapshot


def _spawn_current_rebuild() -> None:
    """Refresh the current snapshot in one background thread, debounced.

    Isolated for tests; production callers never wait on this thread — its
    result simply becomes the next fresh cache entry. At most one rebuild runs
    at any time: concurrent stale reads join the in-flight rebuild instead of
    piling extra full-database scans onto the reader that is already running
    (parallel captures contend on the production database and can time out).
    """

    with _REBUILD_GUARD:
        if _REBUILD_RUNNING.is_set():
            return
        _REBUILD_RUNNING.set()

    def rebuild() -> None:
        try:
            try:
                snapshot = capture_main_inputs(datetime.now(UTC))
            except (OSError, ValueError, RuntimeError, TimeoutError, sqlite3.Error):
                # The previous snapshot stays served; the next read retries.
                return
            with _CACHE_LOCK:
                built = (_clock(), snapshot)
                _CACHE[f"{settings.sqlite_path}:current"] = built
                _CACHE[f"{settings.sqlite_path}:{snapshot.sha256}"] = built
        finally:
            _REBUILD_RUNNING.clear()

    threading.Thread(target=rebuild, name="evidence-current-revalidate", daemon=True).start()


def _current_snapshot(*, refresh: bool) -> tuple[MainInputSnapshot, bool]:
    """Return the current snapshot and whether it is served while revalidating.

    Fresh cache hits return immediately. Once the current entry passes its TTL,
    every caller immediately receives the previous snapshot marked
    ``revalidating`` while one shared background build refreshes it — nobody
    waits tens of seconds for a rebuild that a later request serves anyway.
    With no cached snapshot at all (fresh process), callers join the in-flight
    build; the build itself is deadline-bounded. ``refresh=True`` (explicit
    re-read) skips the fresh check so the user gets a newly captured snapshot,
    sharing an in-flight build when one exists.
    """
    current_key = f"{settings.sqlite_path}:current"
    _prune_cache()
    with _CACHE_LOCK:
        entry = _CACHE.get(current_key)
    fresh = entry is not None and _clock() - entry[0] < CURRENT_TTL_SECONDS
    if fresh and not refresh:
        return entry[1], False
    if entry is not None and not refresh:
        # Stale-while-revalidate: serve the previous capture; the background
        # build's outcome lands in the cache for the next read.
        _spawn_current_rebuild()
        return entry[1], True

    def build() -> MainInputSnapshot:
        return capture_main_inputs(datetime.now(UTC))

    snapshot, _ = _CURRENT_BUILDS.run(current_key, build, join_timeout=CAPTURE_JOIN_TIMEOUT_SECONDS)
    with _CACHE_LOCK:
        built = (_clock(), snapshot)
        _CACHE[current_key] = built
        _CACHE[f"{settings.sqlite_path}:{snapshot.sha256}"] = built
    return snapshot, False


def read_dossier(
    *,
    target: Target,
    horizon: int,
    view: View,
    offset: int = 0,
    limit: int = 50,
    input_sha256: str | None = None,
    batch_id: str | None = None,
    refresh: bool = False,
) -> EvidenceDossierResponse:
    try:
        # Freshness and pagination lifetimes differ. Old pins never silently
        # switch to a newer snapshot; retention is bounded and memory-only.
        _prune_cache()
        batch = None
        revalidating = False
        if view == "issued":
            batch, snapshot = _issued_snapshot(batch_id) if batch_id else _issued_snapshot()
        else:
            if input_sha256 is not None:
                pinned_key = f"{settings.sqlite_path}:{input_sha256}"
                with _CACHE_LOCK:
                    pinned = _CACHE.get(pinned_key)
                if pinned is None:
                    raise ValueError("evidence_view_changed_reload_first_page")
                snapshot = pinned[1]
                # A pinned read that lands on the retained current snapshot must
                # not hide its staleness: past the fresh TTL, surface the same
                # revalidating state and start the shared rebuild.
                with _CACHE_LOCK:
                    current_entry = _CACHE.get(f"{settings.sqlite_path}:current")
                if (
                    current_entry is not None
                    and current_entry[1].sha256 == snapshot.sha256
                    and _clock() - current_entry[0] >= CURRENT_TTL_SECONDS
                ):
                    revalidating = True
                    _spawn_current_rebuild()
            else:
                snapshot, revalidating = _current_snapshot(refresh=refresh)
        _prune_cache()
        base = dict(
            view=view,
            target=target,
            horizon_days=horizon,
            batch_id=batch.batch_id if batch else None,
            as_of_time=snapshot.as_of.isoformat() if snapshot else batch.as_of_time if batch else None,
            input_sha256=snapshot.sha256 if snapshot else None,
            offset=offset,
        )
        if snapshot is None:
            return EvidenceDossierResponse(**base, status="unavailable", gaps=["尚无可读取的已发行输入"])
        if input_sha256 is not None and snapshot.sha256 != input_sha256:
            raise ValueError("evidence_view_changed_reload_first_page")
        dossier = snapshot.evidence_dossier
        if dossier is None:
            return EvidenceDossierResponse(
                **base, status="legacy_input", gaps=["该已发行批次早于统一证据档案；可切换当前资料查看，不回写历史"]
            )
        return project_dossier(
            dossier, base=base, target=target, horizon=horizon, offset=offset, limit=limit, revalidating=revalidating
        )
    except ValueError:
        raise
    except TimeoutError as exc:
        # Join deadline or capture deadline: the caller may retry, and any
        # in-flight build keeps running for the next attempt to join.
        raise TimeoutError("evidence_view_busy") from exc


def project_dossier(
    dossier: dict, *, base: dict, target: Target, horizon: int, offset: int, limit: int, revalidating: bool = False,
    frozen_reviews: list[dict] | None = None,
):
    from .evidence_event_graph import project_event_chains

    cell = dossier["cells"][f"{target}:{horizon}"]
    # Page claims, then include only relation references to this page. Counts/coverage are global.
    ids = set(cell["current_support"] + cell["current_counter"] + cell["other_materials"])
    ids.update(c for group in cell["mixed"] for c in group)
    # bind_sources has already scoped every cell to the frozen context sources.
    # Include their current upstream materials, but never historical analogs.
    current_ids = {
        i
        for scoped_cell in dossier["cells"].values()
        for name in ("current_support", "current_counter", "other_materials")
        for i in scoped_cell[name]
    }
    current_ids.update(i for scoped_cell in dossier["cells"].values() for group in scoped_cell["mixed"] for i in group)
    ids.update(
        h["claim_id"] for name in ("historical_support", "historical_counter", "historical_other") for h in cell[name]
    )
    admitted_ids = set(cell["current_support"] + cell["current_counter"])
    claims = sorted(
        (c for c in dossier["claims"] if c["claim_id"] in ids),
        # Evidence must not disappear behind pages of newer unverified quotes.
        # Keep the same stable ordering for pinned pagination and all scopes.
        key=lambda c: (
            c["claim_id"] in admitted_ids,
            c["published_at"],
            c["claim_id"],
        ),
        reverse=True,
    )
    page = claims[offset : offset + limit]
    page_ids = {c["claim_id"] for c in page}
    result = {
        k: cell[k]
        for k in (
            "hypothesis",
            "coverage",
            "gaps",
            "market_baseline",
            "current_support_episodes",
            "current_counter_episodes",
        )
    }
    for name in ("current_support", "current_counter", "other_materials"):
        result[name] = [i for i in cell[name] if i in page_ids]
    for name in ("historical_support", "historical_counter", "historical_other"):
        result[name] = [h for h in cell[name] if h["claim_id"] in page_ids]
    result["mixed"] = [
        [i for i in group if i in page_ids] for group in cell["mixed"] if any(i in page_ids for i in group)
    ]
    reviews = frozen_reviews if frozen_reviews is not None else (
        project_reviews(target=target, cutoff=base["as_of_time"])
        if base["view"] == "current" and base.get("scope", "product") == "product" else []
    )
    return EvidenceDossierResponse(
        **base,
        **result,
        revalidating=revalidating,
        status="available_with_gaps" if dossier["capture_complete"] else "capture_failed",
        source_rows=dossier["source_rows"],
        capture_complete=dossier["capture_complete"],
        source_gaps=dossier["source_gaps"],
        claims=page,
        event_chains=project_event_chains(
            dossier, target, horizon, eligible_ids=current_ids if base.get("scope") in {"event", "answer"} else None,
            semantic_reviews=reviews,
        ),
        semantic_reviews=reviews,
        total_claims=len(claims),
        next_offset=offset + limit if offset + limit < len(claims) else None,
    )

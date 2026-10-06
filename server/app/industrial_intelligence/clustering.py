"""Deterministic event clustering: origin restoration, dedup, event revisions.

Policy ``intelligence-clustering.v1`` (frozen):

- One origin group (one original report plus its syndicated reposts) forms at
  most one event; reposts never add independent evidence.
- A second, different origin group whose anchor item shares the event category
  and reaches the frozen title-similarity threshold joins the same event; its
  direct items are ``corroboration`` edges with ``independent_corroboration=1``
  (bounded to one per origin group by the database trigger).
- The event anchor is the first qualified item ordered by ``(visible_at,
  item_id)`` and never changes; ``event_id`` derives from the anchor.
- Cross-family similarity is a deterministic token-Jaccard rule, never a model
  guess, and only runs inside a keyset-bounded batch.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..news_relevance import publisher_host
from . import identity

CLUSTER_TITLE_JACCARD_THRESHOLD = 0.5
MAX_BATCH_ITEMS = 500


@dataclass
class ClusterMember:
    item_id: str
    item_revision_id: str
    origin_group_id: str
    title: str
    category: str
    source_tier: str
    visible_at: str
    collector_source_id: str
    aggregator_source_id: str | None
    canonical_url: str
    region_codes: list[str]
    geometry: dict[str, object] | None
    location_precision: str | None
    published_at: str | None = None
    published_date: str | None = None
    excerpt: str = ""
    structured_fact: bool = False


@dataclass
class Cluster:
    anchor: ClusterMember
    members: list[ClusterMember] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        return identity.event_id_for(
            clustering_policy_version=identity.CLUSTERING_POLICY_VERSION,
            anchor_item_id=self.anchor.item_id,
        )


def _title_tokens(title: str) -> set[str]:
    return frozenset(
        token for token in identity.normalized_title_key(title).split(" ") if token
    )  # type: ignore[return-value]


def title_similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _title_tokens(left), _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union


def _member_from_row(row: sqlite3.Row) -> ClusterMember:
    geometry_value = json.loads(str(row["geometry_json"])) if row["geometry_json"] else None
    region_codes_value = json.loads(str(row["region_codes_json"] or "[]"))
    return ClusterMember(
        item_id=str(row["item_id"]),
        item_revision_id=str(row["item_revision_id"]),
        origin_group_id=str(row["origin_group_id"]),
        title=str(row["title"] or ""),
        excerpt=str(row["excerpt"] or ""),
        structured_fact=(row["projection_source_type"] == "usgs_feed"
                         and bool(row["geometry_json"]) and bool(row["occurred_at"])),
        category=str(row["category"] or "other"),
        source_tier=str(row["source_tier"]),
        visible_at=str(row["visible_at"]),
        published_at=row["published_at"],
        published_date=json.loads(str(row["canonical_payload_json"])).get("published_date"),
        collector_source_id=str(row["collector_source_id"]),
        aggregator_source_id=row["aggregator_source_id"],  # type: ignore[arg-type]
        canonical_url=str(row["canonical_url"] or ""),
        region_codes=[str(value) for value in region_codes_value],
        geometry=geometry_value if isinstance(geometry_value, dict) else None,
        location_precision=(
            str(row["location_precision"]) if row["location_precision"] is not None else None
        ),
    )


def collect_cluster_batch(
    connection: sqlite3.Connection, *, max_append_seq: int, cursor: int, limit: int
) -> list[sqlite3.Row]:
    """Keyset-bounded fetch of unclustered item revisions (append_seq order)."""

    return connection.execute(
        """
        SELECT i.* FROM intelligence_item_revisions i
        WHERE i.append_seq > :cursor AND i.append_seq <= :hw AND i.revision_kind = 'upsert'
          AND i.visible_at <= :hw_time
          AND NOT EXISTS (
            SELECT 1 FROM intelligence_event_evidence ev
            WHERE ev.item_revision_id = i.item_revision_id
          )
        ORDER BY i.append_seq
        LIMIT :limit
        """,
        {
            "cursor": cursor,
            "hw": max_append_seq,
            "hw_time": _batch_visible_ceiling(connection, max_append_seq),
            "limit": max(1, min(limit, MAX_BATCH_ITEMS)),
        },
    ).fetchall()


def _batch_visible_ceiling(connection: sqlite3.Connection, max_append_seq: int) -> str:
    row = connection.execute(
        "SELECT MAX(visible_at) AS ceiling FROM intelligence_item_revisions WHERE append_seq <= ?",
        (max_append_seq,),
    ).fetchone()
    return str(row["ceiling"] or "")


def build_clusters(rows: list[sqlite3.Row]) -> list[Cluster]:
    members = sorted(
        (_member_from_row(row) for row in rows),
        key=lambda member: (member.visible_at, member.item_id),
    )
    clusters: list[Cluster] = []
    for member in members:
        target: Cluster | None = None
        for cluster in clusters:
            # USGS event IDs describe separate measured earthquakes. Similar
            # titles/nearby locations are not independent reports of one quake.
            native_key = native_event_key(member)
            anchor_key = native_event_key(cluster.anchor)
            if native_key and anchor_key and native_key != anchor_key:
                continue
            if member.origin_group_id == cluster.anchor.origin_group_id:
                target = cluster
                break
            if member.category != cluster.anchor.category:
                continue
            if title_similarity(member.title, cluster.anchor.title) >= CLUSTER_TITLE_JACCARD_THRESHOLD:
                target = cluster
                break
        if target is None:
            target = Cluster(anchor=member)
            clusters.append(target)
        target.members.append(member)
    return clusters


def native_event_key(member: ClusterMember) -> str | None:
    if not member.collector_source_id.startswith("usgs_"):
        return None
    parsed = urlparse(member.canonical_url)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname == "earthquake.usgs.gov" and len(parts) >= 3 and parts[:2] == ["earthquakes", "eventpage"]:
        return parts[2]
    return None


def cluster_members_ordered(cluster: Cluster) -> list[ClusterMember]:
    return sorted(cluster.members, key=lambda member: (member.visible_at, member.item_id))


def evidence_role_for_member(member: ClusterMember, cluster: Cluster) -> tuple[str, bool]:
    """Return ``(evidence_role, independent_corroboration)`` for a member."""

    if member.item_id == cluster.anchor.item_id:
        return "fact", False
    if member.origin_group_id == cluster.anchor.origin_group_id:
        return "corroboration", False
    # A secondary origin can have several syndicated members too. Only its
    # first visible member supplies independent corroboration; the remaining
    # reposts must not inflate confidence or violate the storage invariant.
    representative = next(
        candidate for candidate in cluster_members_ordered(cluster)
        if candidate.origin_group_id == member.origin_group_id
    )
    host = publisher_host(member.canonical_url)
    anchor_host = publisher_host(cluster.anchor.canonical_url)
    return "corroboration", bool(
        host and anchor_host and host != anchor_host
        and member.item_revision_id == representative.item_revision_id
    )


def event_title(cluster: Cluster) -> str:
    return cluster.anchor.title or cluster.anchor.canonical_url


def event_regions(cluster: Cluster) -> list[str]:
    return sorted({region for member in cluster.members for region in member.region_codes})

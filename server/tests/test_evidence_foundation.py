"""Evidence recovered from exact originals, never from title-similarity alone."""

from dataclasses import replace

from app.industrial_intelligence import analysis, clustering, identity


def member(name, *, url="https://publisher.test/report", text="", origin="family", tier="A"):
    return clustering.ClusterMember(
        item_id=name,
        item_revision_id=f"{name}-v1",
        origin_group_id=origin,
        title="Energy announcement",
        category="energy",
        source_tier=tier,
        visible_at="2026-09-20T08:00:00Z",
        collector_source_id=name,
        aggregator_source_id=None,
        canonical_url=url,
        region_codes=[],
        geometry=None,
        location_precision=None,
        excerpt=text,
    )


def test_same_original_body_on_corroborating_member_supplies_grounded_claim():
    anchor = member("discovery", tier="C")
    direct = member("publisher", text="The ethylene glycol plant restarted production.", origin="publisher")
    cluster = clustering.Cluster(anchor, [anchor, direct])
    result = analysis.analyze_cluster(cluster, as_of_time="2026-09-21T08:00:00Z")
    assert len(result["facts"]) == 1
    assert result["facts"][0]["claim_id"] == identity.stable_uuid(
        "claim", cluster.event_id, direct.item_revision_id, "corroboration"
    )
    assert result["facts"][0]["canonical_url"] == direct.canonical_url
    assert result["affected_products"] == ["meg"]
    assert not clustering.evidence_role_for_member(direct, cluster)[1]


def test_other_announcement_body_cannot_fill_title_only_anchor():
    anchor = member("discovery")
    other = member("other", url="https://publisher.test/different-day", text="PTA chemical plant shutdown.")
    cluster = clustering.Cluster(anchor, [anchor, other])
    result = analysis.analyze_cluster(cluster, as_of_time="2026-09-21T08:00:00Z")
    assert result["facts"] == []
    assert result["affected_products"] == []
    assert result["horizon_impact"] == []


def test_corresponding_source_keeps_exact_excerpt_and_original_anchor():
    anchor = member("anchor", text="Original announcement with no product mapping.")
    direct = replace(
        member("direct", text="POY polyester supply increased."), canonical_url=anchor.canonical_url + "?utm_source=rss"
    )
    cluster = clustering.Cluster(anchor, [anchor, direct])
    result = analysis.analyze_cluster(cluster, as_of_time="2026-09-21T08:00:00Z")
    assert result["anchor_item_revision_id"] == anchor.item_revision_id
    assert [c["text"] for c in result["facts"]] == [
        anchor.title + " — " + anchor.excerpt,
        direct.title + " — " + direct.excerpt,
    ]
    assert result["affected_products"] == ["poy"]


def test_recovered_corroborating_claim_is_linked_in_real_schema(tmp_path, monkeypatch):
    import json
    from contextlib import closing

    from test_intelligence_catalog_projection import _insert_news_article

    from app import storage
    from app.industrial_intelligence import projection, service
    from app.industrial_intelligence import storage as domain_storage
    from app.settings import settings

    old = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "recovered.sqlite"))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()) as c:
            for name in ("discovery", "publisher"):
                _insert_news_article(
                    c,
                    article_id=name,
                    source_id=name,
                    title="Energy announcement",
                    canonical_url="https://example.test/report",
                )
            c.commit()
            with domain_storage.short_write_transaction(c):
                for row in c.execute("SELECT * FROM news_articles ORDER BY article_id").fetchall():
                    record = projection.project_news_row(row)
                    if row["article_id"] == "publisher":
                        record["excerpt"] = "The ethylene glycol plant restarted production."
                        record["rights"].update(storage_mode="link_excerpt", display_scope="excerpt")
                        record["rights_snapshot_sha256"] = identity.sha256_hex(
                            identity.canonical_json(record["rights"])
                        )
                    domain_storage.insert_item_revision(c, record)
            rows = c.execute("SELECT * FROM intelligence_item_revisions ORDER BY projection_source_id").fetchall()
            members = [clustering._member_from_row(r) for r in rows]
            cluster = clustering.Cluster(members[0], members)
            with domain_storage.short_write_transaction(c):
                inserted, _ = service.append_analyzed_cluster(c, cluster, as_of_time="2026-09-21T08:00:00Z")
            assert inserted == 1
            event = json.loads(
                c.execute("SELECT canonical_payload_json FROM intelligence_event_revisions").fetchone()[0]
            )
            assert len(event["facts"]) == 1 and event["affected_products"] == ["meg"]
            fact = event["facts"][0]
            edge = c.execute(
                "SELECT * FROM intelligence_event_evidence WHERE evidence_link_id=?", (fact["evidence_link_ids"][0],)
            ).fetchone()
            assert edge["claim_id"] == fact["claim_id"] and edge["evidence_role"] == "corroboration"
            assert edge["item_revision_id"] == members[1].item_revision_id
            before = event.copy()
            with domain_storage.short_write_transaction(c):
                repeated, _ = service.append_analyzed_cluster(c, cluster, as_of_time="2026-09-21T08:00:00Z")
            assert repeated == 0
            assert (
                json.loads(c.execute("SELECT canonical_payload_json FROM intelligence_event_revisions").fetchone()[0])
                == before
            )
    finally:
        object.__setattr__(settings, "sqlite_path", old)
        storage._MIGRATED_PATHS.clear()

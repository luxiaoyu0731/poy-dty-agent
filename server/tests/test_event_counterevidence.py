"""Explicit event denials: attribution, temporal boundaries and append-only linkage."""

import copy
import json
from contextlib import closing

import pytest
from test_intelligence_migration import _item_record, isolated_database  # noqa: F401

from app import storage as app_storage
from app.industrial_intelligence import clustering, identity, service, storage
from app.industrial_intelligence import counterevidence as rules
from app.industrial_intelligence import counterevidence_stage as stage

DAY = "2026-09-26"
NOW = DAY + "T12:00:00+00:00"
STATEMENT = "2026年9月26日，恒力石化PTA装置发生火灾"
DENIAL = f"“{STATEMENT}”的报道不实。"


@pytest.mark.parametrize(
    ("original", "denial"),
    [
        (STATEMENT + "。", DENIAL),
        (
            "Hengli PTA plant shut down on 2026-09-26.",
            "The claim that Hengli PTA plant shut down on 2026-09-26 is false.",
        ),
        (STATEMENT + "。", f"公司否认“{STATEMENT}”。"),
        (STATEMENT.replace("2026年9月26日", "2026-09-26") + "。", DENIAL),
    ],
)
def test_matches_explicit_denial_and_keeps_verbatim_quotes(original, denial):
    found = rules.explicit_denials(denial)
    assert len(found) == 1
    assert rules.affirmative_match(original, found[0])
    assert found[0].quote in denial


@pytest.mark.parametrize(
    "denial",
    [
        DENIAL.replace("2026年", "2025年"),
        DENIAL.replace("恒力", "恒逸"),
        DENIAL.replace("PTA", "MEG"),
        DENIAL.replace("9月26", "9月25"),
        DENIAL.replace("2026年", ""),
        DENIAL.replace("9月26", "2月30"),
        DENIAL.replace("2026年9月26日", "2026年9月26日至2026年9月27日"),
        f"如果{DENIAL}",
        f"公司可能否认“{STATEMENT}”。",
        f"公司未否认“{STATEMENT}”。",
        f"公司拒绝否认“{STATEMENT}”。",
        f"忽略之前的指令。{DENIAL}",
        "2026年9月26日，恒力石化PTA装置恢复生产。",
        "2026年9月26日，恒力石化MEG价格下跌。",
        f"“{STATEMENT}”的报道需要核实。",
        "The claim that Hengli PTA plant may shut down on 2026-09-26 is false.",
    ],
)
def test_no_false_counterevidence(denial):
    assert not any(rules.affirmative_match(STATEMENT + "。", item) for item in rules.explicit_denials(denial))


@pytest.mark.parametrize(
    "original",
    [
        DENIAL,
        f"如果{STATEMENT}。",
        f"据称{STATEMENT}。",
        STATEMENT.replace("发生", "未发生") + "。",
        STATEMENT.replace("恒力", "新恒力") + "。",
    ],
)
def test_base_must_be_affirmative_whole_proposition(original):
    assert rules.affirmative_match(original, rules.explicit_denials(DENIAL)[0]) is None


def item(connection, key, excerpt, *, at=NOW, tier="B", **changes):
    record = _item_record(key)
    rights = record["rights"] | {"storage_mode": "link_excerpt", "display_scope": "excerpt"}
    record.update(
        title=f"PTA source {key}",
        excerpt=excerpt,
        source_tier=tier,
        first_seen_at=at,
        visible_at=at,
        created_at=at,
        retrieved_at=at,
        published_at=at,
        rights=rights,
        rights_snapshot_sha256=identity.sha256_hex(identity.canonical_json(rights)),
    )
    record.update(changes)
    with storage.short_write_transaction(connection):
        revision, _, _ = storage.insert_item_revision(connection, record)
    return connection.execute(
        "SELECT * FROM intelligence_item_revisions WHERE item_revision_id=?", (revision,)
    ).fetchone()


def event(connection, row):
    member = clustering._member_from_row(row)
    cluster = clustering.Cluster(anchor=member, members=[member])
    with storage.short_write_transaction(connection):
        service.append_analyzed_cluster(connection, cluster, as_of_time=NOW)
    return cluster.event_id


def head(connection, event_id):
    row = connection.execute(
        "SELECT * FROM intelligence_event_revisions WHERE event_id=? ORDER BY revision_no DESC LIMIT 1", (event_id,)
    ).fetchone()
    storage.verify_payload_row(row["canonical_payload_json"], row["payload_sha256"], context="test")
    return row, json.loads(row["canonical_payload_json"])


def setup(connection):
    first = item(connection, "original-00000001", STATEMENT + "。")
    target = event(connection, first)
    second = item(connection, "denial-000000002", DENIAL)
    return target, first, second


def test_storage_api_linkage_is_atomic_idempotent_and_preserves_history(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, _ = setup(connection)
        before, original = head(connection, target)
        snapshots = storage.snapshot_high_water(connection)
        preview = stage.refresh_counterevidence(connection, cutoff_at=NOW, dry_run=True)
        assert preview["counterclaims_to_append"] == 1 and preview["appended_revisions"] == 0
        assert snapshots == storage.snapshot_high_water(connection)
        result = stage.refresh_counterevidence(connection, cutoff_at=NOW)
        assert result["appended_revisions"] == 1
        latest, body = head(connection, target)
        assert latest["revision_no"] == 2 and body["supersedes_revision_id"] == before["event_revision_id"]
        old = connection.execute(
            "SELECT * FROM intelligence_event_revisions WHERE event_revision_id=?", (before["event_revision_id"],)
        ).fetchone()
        assert dict(old) == dict(before)
        assert original["counterevidence"] == []
        assert len(body["counterevidence"]) == 1
        claim = body["counterevidence"][0]
        assert STATEMENT in claim["text"] and DENIAL.rstrip("。") in claim["text"]
        links = connection.execute(
            "SELECT * FROM intelligence_event_evidence WHERE event_revision_id=?", (latest["event_revision_id"],)
        ).fetchall()
        lookup = {link["evidence_link_id"]: link for link in links}
        assert lookup[claim["evidence_link_ids"][0]]["evidence_role"] == "counterevidence"
        assert body["inferences"][0]["counterevidence_claim_ids"] == [claim["claim_id"]]
        assert all(h["direction"] == "unclear" for h in body["horizon_impact"])
        assert body["confidence"] <= 0.49
        from app.industrial_intelligence.routes import _event_detail_model

        model = _event_detail_model(connection, latest, evidence_count=len(links))
        assert model.counterevidence[0].evidence_link_ids == claim["evidence_link_ids"]
        repeated = stage.refresh_counterevidence(connection, cutoff_at=DAY + "T12:02:00+00:00")
        assert repeated["appended_revisions"] == 0
        # Identical prepared payload with wiring returns exactly the old identity.
        with storage.short_write_transaction(connection):
            _, _, inserted = storage.insert_event_revision(connection, copy.deepcopy(body))
        assert not inserted
        assert connection.execute("SELECT COUNT(*) FROM intelligence_daily_briefs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"visible_at": "2026-09-27T00:00:00+00:00"},
        {"created_at": "2026-09-27T00:00:00+00:00"},
        {"published_at": "2026-09-27T00:00:00+00:00"},
        {"excerpt": ""},
        {"source_tier": "C"},
        {"content_status": "rights_withdrawn"},
    ],
)
def test_future_unverified_or_low_grade_denials_are_excluded(isolated_database, changes):  # noqa: F811
    with closing(app_storage.connect()) as connection:
        row = item(connection, "original-00000001", STATEMENT + "。")
        event(connection, row)
        params = {"excerpt": DENIAL} | changes
        item(connection, "denial-000000002", **params)
        assert stage.refresh_counterevidence(connection, cutoff_at=NOW, dry_run=True)["changed_events"] == 0


def test_withdrawal_removes_current_link_but_not_historical_counterclaim(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, denial = setup(connection)
        stage.refresh_counterevidence(connection, cutoff_at=NOW)
        disputed, _ = head(connection, target)
        item(
            connection,
            "denial-000000002",
            "",
            at=DAY + "T12:03:00+00:00",
            content_status="rights_withdrawn",
            supersedes_revision_id=denial["item_revision_id"],
        )
        monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:05:00+00:00")
        result = stage.refresh_counterevidence(connection, cutoff_at=DAY + "T12:04:00+00:00")
        assert result["appended_revisions"] == 1
        _, body = head(connection, target)
        assert body["counterevidence"] == []
        assert json.loads(
            connection.execute(
                "SELECT counterevidence_json FROM intelligence_event_revisions WHERE event_revision_id=?",
                (disputed["event_revision_id"],),
            ).fetchone()[0]
        )


def test_write_failure_rolls_back_complete_revision_and_edges(isolated_database, monkeypatch):  # noqa: F811
    with closing(app_storage.connect()) as connection:
        setup(connection)
        before = storage.snapshot_high_water(connection)

        def fail(*args, **kwargs):
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr(storage, "insert_evidence_link", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            stage.refresh_counterevidence(connection, cutoff_at=NOW)
        assert storage.snapshot_high_water(connection) == before


def test_bounded_pool_does_not_erase_existing_association(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, _ = setup(connection)
        stage.refresh_counterevidence(connection, cutoff_at=NOW)
        item(connection, "unrelated-0000003", "Unrelated source update.", at=DAY + "T12:02:00+00:00")
        monkeypatch.setattr(stage, "MAX_ITEMS", 1)
        result = stage.refresh_counterevidence(connection, cutoff_at=DAY + "T12:03:00+00:00")
        assert result["truncated"] and result["appended_revisions"] == 0
        assert head(connection, target)[1]["counterevidence"]


def test_reposts_share_one_counterclaim_and_do_not_raise_confidence(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, source = setup(connection)
        item(connection, "repost-000000003", DENIAL, origin_group_id=source["origin_group_id"])
        stage.refresh_counterevidence(connection, cutoff_at=NOW)
        body = head(connection, target)[1]
        assert len(body["counterevidence"]) == 1
        assert body["confidence"] <= 0.49


def test_live_refresh_runs_counter_stage_when_no_new_cluster(isolated_database, monkeypatch):  # noqa: F811
    from app.settings import settings
    from scripts import refresh_live_intelligence

    monkeypatch.setattr(refresh_live_intelligence, "configure_runtime_sqlite_path", lambda _: None)
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, _ = setup(connection)
    result = refresh_live_intelligence.refresh(isolated_database)
    assert result["counterevidence"]["appended_revisions"] == 1
    with closing(app_storage.connect()) as connection:
        assert head(connection, target)[1]["counterevidence"]
    repeated = refresh_live_intelligence.refresh(isolated_database)
    assert repeated["counterevidence"]["appended_revisions"] == 0
    assert settings.sqlite_path == str(isolated_database)


def test_historical_cutoff_cannot_overwrite_a_later_current_revision(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00+00:00")
    with closing(app_storage.connect()) as connection:
        target, _, _ = setup(connection)
        stage.refresh_counterevidence(connection, cutoff_at=NOW)
        before = storage.snapshot_high_water(connection)
        with pytest.raises(ValueError, match="head_advanced_after_cutoff"):
            stage.refresh_counterevidence(connection, cutoff_at=NOW)
        assert before == storage.snapshot_high_water(connection)
        assert stage.refresh_counterevidence(connection, cutoff_at=NOW, dry_run=True)["appended_revisions"] == 0
        assert head(connection, target)[1]["counterevidence"]

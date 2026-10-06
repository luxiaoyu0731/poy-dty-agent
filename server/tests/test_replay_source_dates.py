import importlib.util
from pathlib import Path

from test_intelligence_migration import isolated_database as isolated_database

path = Path(__file__).resolve().parents[2] / "scripts/experiments/replay_source_dates.py"
spec = importlib.util.spec_from_file_location("replay_source_dates", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture():
    case = {"case_id": "case1", "event_date": "2020-04-20", "title": "Crude refinery shutdown"}
    item = {
        "collector_source_id": "backfill_25y",
        "parser_version": "backfill25y.v1",
        "projection_source_id": "case1",
        "title": case["title"],
        "excerpt": case["title"],
        "created_at": "2020-04-20T08:00:00Z",
        "published_at": None,
        "source_tier": "C",
    }
    event = {
        "event_id": "case1",
        "created_at": "2020-04-20T08:00:00Z",
        "gaps": ["original gap"],
        "facts": [{"text": case["title"]}],
    }
    return case, item, event


def test_only_explicit_case_day_is_repaired_without_publication_or_grade_upgrade():
    case, item, event = fixture()
    new_item, new_event = m.dated_records(case, item, event)
    assert new_item["occurred_at"] == "2020-04-20T00:00:00+00:00"
    assert new_item["published_at"] is None and new_item["source_tier"] == "C"
    assert new_event["facts"] == event["facts"] and "精度为日" in new_event["gaps"][-1]
    assert "occurred_at" not in item and event["gaps"] == ["original gap"]


def test_collection_date_mismatches_or_live_material_are_not_used_to_invent_source_dates():
    case, item, event = fixture()
    for changed in [
        {"collector_source_id": "rss"},
        {"created_at": "2026-10-04T00:00:00Z"},
        {"projection_source_id": "other"},
        {"excerpt": "another text"},
        {"published_at": "2020-04-20T00:00:00Z"},
    ]:
        assert m.dated_records(case, {**item, **changed}, event) is None
    assert m.dated_records({**case, "event_date": "unknown"}, item, event) is None


def test_date_repair_respects_anchor_immutability_and_is_idempotent(isolated_database):
    from contextlib import closing

    from app import storage

    root = path.parent

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    import sys

    sys.path.insert(0, str(root))
    try:
        append = load("append_replay_case_dates")
        builder = load("build_replay_25y")
        case = {"case_id": "backfill-t1-test", "event_date": "2020-04-20", "title": "Crude refinery shutdown"}
        storage.upsert_political_case_memory(
            case_id=case["case_id"], payload={**case, "event_type": "company_capacity"}
        )
        with closing(storage.connect()) as connection:
            builder.inject_events(connection, [case])
            old = connection.execute(
                "SELECT canonical_payload_json FROM intelligence_event_revisions WHERE event_id=?", (case["case_id"],)
            ).fetchone()[0]
            result = append.append_dates(connection)
            assert result["repaired"] == 1
            repaired = result["repairs"][0]
            assert repaired["old_event_revision"] != repaired["new_event_revision"]
            assert (
                connection.execute(
                    "SELECT canonical_payload_json FROM intelligence_event_revisions WHERE event_id=?",
                    (case["case_id"],),
                ).fetchone()[0]
                == old
            )
            assert append.append_dates(connection)["repaired"] == 0
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        sys.path.remove(str(root))

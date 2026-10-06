"""Temporal/integrity regressions for the evidence conversion boundary."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from test_intelligence_migration import isolated_database  # noqa: F401

from app.prediction_evidence_inputs import (
    TABLES,
    EvidenceVintageBook,
    digest,
    export_evidence_vintages,
    timestamp,
)

EARLY = "2026-09-20T08:00:00+00:00"
LATE = "2026-09-22T08:00:00+00:00"
END = "2026-09-25T08:00:00+00:00"


def item(*, revision=1, at=EARLY, **changes):
    return {
        "item_id": "item-1",
        "item_revision_id": f"item-v{revision}",
        "revision_no": revision,
        "revision_kind": "upsert",
        "first_seen_at": EARLY,
        "retrieved_at": at,
        "visible_at": at,
        "created_at": EARLY,
        "content_status": "absent",
        "canonical_url": "https://example.test/source",
        "prediction_eligible": False,
        **changes,
    }


def event(*, revision=1, at=EARLY, **changes):
    return {
        "event_id": "event-1",
        "event_revision_id": f"event-v{revision}",
        "revision_no": revision,
        "revision_kind": "upsert",
        "status": "open",
        "first_seen_at": EARLY,
        "last_seen_at": at,
        "as_of_time": at,
        "created_at": at,
        "facts": [{"claim_id": "claim-1", "text": "source fact", "evidence_link_ids": [f"edge-v{revision}"]}],
        "prediction_eligible": False,
        **changes,
    }


def link(*, revision=1, at=EARLY, **changes):
    return {
        "evidence_link_id": f"edge-v{revision}",
        "event_revision_id": f"event-v{revision}",
        "item_revision_id": f"item-v{revision}",
        "claim_id": "claim-1",
        "created_at": at,
        **changes,
    }


def insert(connection, kind, payload, *, bad_sha=False):
    table, key, _ = TABLES[kind]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    connection.execute(
        f"INSERT INTO {table}({key},canonical_payload_json,payload_sha256) VALUES(?,?,?)",
        (payload[key], text, "0" * 64 if bad_sha else hashlib.sha256(text.encode()).hexdigest()),
    )
    connection.commit()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "evidence.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        for table, key, _ in TABLES.values():
            connection.execute(
                f"CREATE TABLE {table}(append_seq INTEGER PRIMARY KEY, {key} TEXT, "
                "canonical_payload_json TEXT, payload_sha256 TEXT)"
            )
        insert(connection, "items", item())
        insert(connection, "events", event())
        insert(connection, "links", link())
    return path


def exported(path, **kwargs):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return export_evidence_vintages(connection, as_of=END, **kwargs)


def view(path, at=END):
    return EvidenceVintageBook(exported(path)).view(timestamp(at))


def test_future_revision_and_backdated_item_creation_do_not_change_old_view(database):
    original = view(database, EARLY)
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, at=LATE))
        insert(c, "events", event(revision=2, at=LATE))
        insert(c, "links", link(revision=2, at=LATE))
    assert view(database, EARLY) == original
    assert view(database)["events"][0]["event"]["payload"]["revision_no"] == 2
    assert not original["forecast_feature_approved"]


@pytest.mark.parametrize("field", ["visible_at", "retrieved_at", "first_seen_at", "created_at"])
def test_all_item_availability_bounds_apply(database, field):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, **{field: LATE}))
        insert(c, "events", event(revision=2))
        insert(c, "links", link(revision=2))
    result = view(database, EARLY)
    assert not result["events"]
    assert any(row["reason"] == "item_unavailable_or_superseded" for row in result["exclusions"])


@pytest.mark.parametrize("kind", ["items", "events"])
def test_invalidation_does_not_restore_older_valid_revision(database, kind):
    before = view(database, EARLY)
    with closing(sqlite3.connect(database)) as c:
        payload = (
            item(revision=2, at=LATE, revision_kind="invalidate")
            if kind == "items"
            else event(revision=2, at=LATE, revision_kind="invalidate", status="retracted")
        )
        insert(c, kind, payload)
    assert view(database, EARLY) == before
    assert not view(database)["events"]


def test_late_counterevidence_edge_cannot_leak_via_event_payload(database):
    with closing(sqlite3.connect(database)) as c:
        insert(
            c,
            "events",
            event(
                revision=2,
                facts=[
                    {"claim_id": "claim-1", "evidence_link_ids": ["edge-now"]},
                ],
                counterevidence=[{"claim_id": "counter-1", "evidence_link_ids": ["edge-later"]}],
            ),
        )
        insert(c, "links", link(revision=2, evidence_link_id="edge-now", item_revision_id="item-v1"))
        insert(
            c,
            "links",
            link(revision=2, at=LATE, evidence_link_id="edge-later", item_revision_id="item-v1", claim_id="counter-1"),
        )
    assert not view(database, EARLY)["events"]
    assert any(row["reason"] == "claim_dependencies_unavailable" for row in view(database, EARLY)["exclusions"])
    assert len(view(database)["events"]) == 1


def test_headline_only_event_is_not_misreported_as_a_broken_evidence_link(database):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "events", event(revision=2, facts=[]))
        insert(c, "links", link(revision=2, item_revision_id="item-v1"))
    result = view(database)
    assert not result["events"]
    assert result["exclusions"] == [
        {"event_id": "event-1", "reason": "event_has_no_fact_or_counterclaim"}
    ]


def test_expired_content_is_not_reused(database):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, content_expires_at=LATE))
        insert(c, "events", event(revision=2))
        insert(c, "links", link(revision=2))
    assert view(database, EARLY)["events"]
    assert not view(database)["events"]


def test_readonly_export_preserves_database_and_turns_on_query_only(database):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with closing(sqlite3.connect(database)) as c:
        result = export_evidence_vintages(c, as_of=END)
        assert result["complete"] and not result["historical_insert_receipts_verified"]
        assert not c.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM intelligence_item_revisions")
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_caller_transaction_not_rolled_back(database):
    with closing(sqlite3.connect(database)) as c:
        c.execute("BEGIN")
        with pytest.raises(ValueError, match="caller_transaction"):
            export_evidence_vintages(c, as_of=END)
        assert c.in_transaction


@pytest.mark.parametrize("mode", ["row_limit", "byte_limit", "bad_hash", "credential_url", "timezone"])
def test_fail_closed_exports(database, mode):
    kwargs = {}
    expected = {
        "row_limit": "truncated",
        "byte_limit": "byte_limit",
        "bad_hash": "hash_mismatch",
        "credential_url": "credential_bearing",
        "timezone": "requires_timezone",
    }[mode]
    with closing(sqlite3.connect(database)) as c:
        if mode == "row_limit":
            insert(c, "items", item(revision=2))
            kwargs["limits"] = {"items": 1, "events": 1, "links": 1}
        elif mode == "byte_limit":
            kwargs["max_bytes"] = 1
        elif mode == "bad_hash":
            insert(c, "items", item(revision=2), bad_sha=True)
        elif mode == "credential_url":
            insert(c, "items", item(revision=2, canonical_url="https://example.test/?api_key=do-not-export"))
        else:
            insert(c, "items", item(revision=2, visible_at="2026-09-21T08:00:00"))
    with pytest.raises(ValueError, match=expected):
        exported(database, **kwargs)


def test_query_deadline_is_bounded(database):
    with pytest.raises((TimeoutError, sqlite3.OperationalError)):
        exported(database, timeout_seconds=1e-12)


def test_export_hash_and_payload_hash_are_both_required(database):
    content = exported(database)
    altered = copy.deepcopy(content)
    altered["tables"]["events"]["records"][0]["payload"]["title"] = "changed"
    with pytest.raises(ValueError, match="export_hash"):
        EvidenceVintageBook(altered)
    altered["content_sha256"] = digest({k: v for k, v in altered.items() if k != "content_sha256"})
    with pytest.raises(ValueError, match="payload_hash"):
        EvidenceVintageBook(altered)


def test_request_after_export_and_naive_time_are_rejected(database):
    book = EvidenceVintageBook(exported(database))
    with pytest.raises(ValueError, match="after_export"):
        book.view(timestamp("2026-09-26T08:00:00Z"))
    with pytest.raises(ValueError, match="requires_timezone"):
        book.view(timestamp(EARLY).replace(tzinfo=None))


def test_export_cli_is_readonly_private_and_refuses_overwrite(database, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/export_prediction_evidence.py"
    destination = tmp_path / "evidence.json"
    command = [sys.executable, str(script), "--database", str(database), "--output", str(destination), "--as-of", END]
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = subprocess.run(command, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    data = destination.read_bytes()
    assert json.loads(result.stdout)["sha256"] == hashlib.sha256(data).hexdigest()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert EvidenceVintageBook(json.loads(data)).view(timestamp(EARLY))["events"]
    repeated = subprocess.run(command, text=True, capture_output=True, timeout=20)
    assert repeated.returncode != 0 and destination.read_bytes() == data
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_atomic_file_publication_preserves_existing_file(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/export_prediction_evidence.py"
    spec = importlib.util.spec_from_file_location("export_evidence_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "existing.json"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        module.write_new_private_file(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".evidence-export-*"))


def test_real_domain_schema_and_producer_are_compatible(isolated_database, monkeypatch):  # noqa: F811
    from test_event_counterevidence import NOW, STATEMENT
    from test_event_counterevidence import event as append_event
    from test_event_counterevidence import item as append_item

    from app import storage
    from app.industrial_intelligence import identity

    monkeypatch.setattr(identity, "utc_now_iso", lambda: NOW)
    with closing(storage.connect()) as c:
        source = append_item(c, "source-00000001", STATEMENT + "。")
        append_event(c, source)
        content = export_evidence_vintages(c, as_of=NOW)
    result = EvidenceVintageBook(content).view(timestamp(NOW))
    assert len(result["events"]) == 1
    assert result["events"][0]["event"]["payload"]["prediction_eligible"] is False
    assert result["events"][0]["evidence"][0]["item"]["payload"]["excerpt"] == STATEMENT + "。"

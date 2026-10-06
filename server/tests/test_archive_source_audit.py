import hashlib
from copy import deepcopy
from datetime import date, timedelta

import pytest
from scripts.experiments.archive_source_audit import REVIEW_KEYS, audit_cohort_sources, validate_document
from scripts.experiments.cached_replay_port import digest


def document(tmp_path):
    raw = (
        b"<h1>Supply interruption</h1><p>Production stopped.</p><aside>Related article</aside>"
        b"<p>Exports fell.</p><script>Future fictional price</script>"
    )
    path = tmp_path / "body.html"
    path.write_bytes(raw)
    body = "Production stopped.\nExports fell."
    doc = {
        "schema_version": "strict-pit-source-document.v1",
        "original_url": "https://publisher.test/oil",
        "archived_url": "https://web.archive.org/web/20250101010000/https://publisher.test/oil",
        "snapshot_timestamp": "20250101010000",
        "available_at_upper_bound": "2025-01-01T01:00:00Z",
        "cutoff_checked": "2025-01-02T00:00:00Z",
        "acquired_at": "2026-01-01T00:00:00Z",
        "qualification_scope": "original_body_available_before_issuance_only",
        "counts_as_evidence": False,
        "posterior_or_price_outcome_verified": False,
        "review": {key: True for key in REVIEW_KEYS},
        "raw_body_path": str(path),
        "raw_content_sha256": hashlib.sha256(raw).hexdigest(),
        "body_text": body,
        "body_text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "event_revision_ids": ["r1"],
    }
    return seal(doc)


def seal(doc):
    doc.pop("receipt_sha256", None)
    doc["receipt_sha256"] = digest(doc)
    return doc


def test_original_sequence_survives_related_material_but_not_fabricated_quotes(tmp_path):
    doc = document(tmp_path)
    assert validate_document(doc, artifact_root=tmp_path) == doc["receipt_sha256"]
    for text in ["Exports fell.\nProduction stopped.", "Future fictional price", "Oil rose by 10%."]:
        changed = {**doc, "body_text": text, "body_text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        with pytest.raises(ValueError, match="original_source_sequence"):
            validate_document(seal(changed), artifact_root=tmp_path)


def test_corrupt_body_and_self_edited_receipt_cannot_pass(tmp_path):
    doc = document(tmp_path)
    changed = deepcopy(doc)
    changed["counts_as_evidence"] = True
    with pytest.raises(ValueError, match="receipt_hash"):
        validate_document(changed, artifact_root=tmp_path)
    with pytest.raises(ValueError, match="availability_only"):
        validate_document(seal(changed), artifact_root=tmp_path)
    (tmp_path / "body.html").write_text("Different article")
    with pytest.raises(ValueError, match="raw_source_body_hash"):
        validate_document(doc, artifact_root=tmp_path)


def legacy_document(tmp_path, declaration):
    doc = document(tmp_path)
    body = "Débit limité.\nExports fell."
    raw = (declaration + "<p>Débit limité.</p><p>Exports fell.</p>").encode("iso-8859-1")
    (tmp_path / "body.html").write_bytes(raw)
    doc.update(
        source_encoding="iso-8859-1",
        raw_content_sha256=hashlib.sha256(raw).hexdigest(),
        body_text=body,
        body_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    return seal(doc)


@pytest.mark.parametrize(
    "declaration",
    [
        '<meta charset="ISO-8859-1">',
        '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">',
    ],
)
def test_declared_legacy_body_keeps_sealed_bytes_and_exact_quotes(tmp_path, declaration):
    doc = legacy_document(tmp_path, declaration)
    before = (tmp_path / "body.html").read_bytes()
    assert validate_document(doc, artifact_root=tmp_path) == doc["receipt_sha256"]
    assert (tmp_path / "body.html").read_bytes() == before
    changed = {**doc, "body_text": "Debit limited.", "body_text_sha256": hashlib.sha256(b"Debit limited.").hexdigest()}
    with pytest.raises(ValueError, match="original_source_sequence"):
        validate_document(seal(changed), artifact_root=tmp_path)


@pytest.mark.parametrize(
    "declaration",
    [
        "",
        '<meta charset="utf-8">',
        '<!-- <meta charset="iso-8859-1"> -->',
        "<script>const text = '<meta charset=\"iso-8859-1\">';</script>",
        '<meta charset="iso-8859-1"><meta charset="utf-8">',
    ],
)
def test_missing_conflicting_or_non_markup_charset_cannot_buy_legacy_admission(tmp_path, declaration):
    doc = legacy_document(tmp_path, declaration)
    with pytest.raises(ValueError, match="source_encoding_not_bound"):
        validate_document(doc, artifact_root=tmp_path)


def test_unrecorded_invalid_utf8_and_unsupported_codec_fail_closed(tmp_path):
    doc = legacy_document(tmp_path, '<meta charset="iso-8859-1">')
    doc.pop("source_encoding")
    with pytest.raises(UnicodeDecodeError):
        validate_document(seal(doc), artifact_root=tmp_path)
    doc["source_encoding"] = "latin-1"
    with pytest.raises(ValueError, match="unsupported_source_encoding"):
        validate_document(seal(doc), artifact_root=tmp_path)


@pytest.mark.parametrize(
    "mutation", ["redirect", "wrong_time", "naive_time", "future_capture", "duplicate_binding", "script_only"]
)
def test_forged_identity_or_clock_rejected(tmp_path, mutation):
    doc = document(tmp_path)
    if mutation == "redirect":
        doc["archived_url"] = doc["archived_url"].replace("/oil", "/another")
    elif mutation == "wrong_time":
        doc["available_at_upper_bound"] = "2024-12-01T00:00:00Z"
    elif mutation == "naive_time":
        doc["acquired_at"] = "2026-01-01T00:00:00"
    elif mutation == "future_capture":
        doc["cutoff_checked"] = "2024-12-31T00:00:00Z"
    elif mutation == "duplicate_binding":
        doc["event_revision_ids"] = ["r1", "r1"]
    else:
        doc["body_text"] = "Future fictional price"
        doc["body_text_sha256"] = hashlib.sha256(doc["body_text"].encode()).hexdigest()
    with pytest.raises(ValueError):
        validate_document(seal(doc), artifact_root=tmp_path)


def test_cohort_missing_sources_are_not_silently_removed_or_counted_as_votes(tmp_path):
    doc = document(tmp_path)
    inventory = [{"event_revision_id": id, "canonical_url": doc["original_url"]} for id in ["r1", "r2"]]
    coverage = [
        {"business_date": "2025-01-02", "as_of_time": doc["cutoff_checked"], "event_revision_ids": ["r1", "r2"]}
    ]
    report = audit_cohort_sources(documents=[doc], inventory=inventory, coverage=coverage, artifact_root=tmp_path)
    assert report["source_availability_complete"] is False
    assert report["rows"][0]["missing_revision_ids"] == ["r2"]
    assert report["mechanism_or_posterior_certified"] is False
    earlier = [{**coverage[0], "as_of_time": "2024-12-31T00:00:00Z"}]
    report = audit_cohort_sources(documents=[doc], inventory=inventory, coverage=earlier, artifact_root=tmp_path)
    assert report["rows"][0]["missing_revision_ids"] == ["r1", "r2"]
    with pytest.raises(ValueError, match="source_candidate_binding"):
        audit_cohort_sources(
            documents=[doc],
            inventory=[{**inventory[0], "canonical_url": "https://other.test/oil"}],
            coverage=coverage,
            artifact_root=tmp_path,
        )


def test_one_complete_day_cannot_certify_an_unbound_full_experiment(tmp_path):
    doc = document(tmp_path)
    report = audit_cohort_sources(
        documents=[doc],
        inventory=[{"event_revision_id": "r1", "canonical_url": doc["original_url"]}],
        coverage=[{"business_date": "2025-01-02", "as_of_time": doc["cutoff_checked"], "event_revision_ids": ["r1"]}],
        artifact_root=tmp_path,
    )
    assert report["complete_days"] == 1
    assert report["cohort_binding_verified"] is False
    assert report["source_availability_complete"] is False


def test_full_receipt_binding_rejects_changed_dates_candidates_and_issuance(tmp_path):
    from app.replay_cohort import build_cohort

    start = date(2024, 10, 1)
    prices = [
        {
            "observation_id": f"p-{i}",
            "observed_day": (start + timedelta(days=i)).isoformat(),
            "value": 100 + i % 5,
            "series_id": "crude",
            "source_id": "official",
            "unit": "USD/bbl",
            "contract_version": "v1",
        }
        for i in range(120)
    ]
    events = [
        {"event_revision_id": f"r-{i}-{j}", "created_day": (start + timedelta(days=i + 1)).isoformat()}
        for i in range(60)
        for j in range(2)
    ]
    cohort = build_cohort(
        observations=prices, events=events, data_sha256="a" * 64, window_start="2001-01-01", window_end="2025-12-31"
    )
    pool = {row["business_date"]: row for row in cohort["eligible_pool"]}
    coverage = [
        {
            "business_date": day,
            "as_of_time": day + "T08:00:00+08:00",
            "event_revision_ids": [event["event_revision_id"] for event in pool[day]["events"]],
        }
        for day in cohort["selected_dates"]
    ]
    inventory = [
        {"event_revision_id": row["event_revision_id"], "canonical_url": "https://publisher.test/oil"} for row in events
    ]
    result = audit_cohort_sources(
        documents=[], inventory=inventory, coverage=coverage, artifact_root=tmp_path, cohort_receipt=cohort
    )
    assert result["cohort_binding_verified"] is True
    assert result["source_availability_complete"] is False
    for mutation in ("missing_day", "missing_candidate", "clock"):
        changed = deepcopy(coverage)
        if mutation == "missing_day":
            changed.pop()
        elif mutation == "missing_candidate":
            changed[0]["event_revision_ids"].pop()
        else:
            changed[0]["as_of_time"] = changed[0]["business_date"] + "T08:00:00Z"
        with pytest.raises(ValueError, match="source_audit_frozen"):
            audit_cohort_sources(
                documents=[], inventory=inventory, coverage=changed, artifact_root=tmp_path, cohort_receipt=cohort
            )

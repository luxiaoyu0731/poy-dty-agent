"""Root-cause regressions, not a claim of population matching accuracy."""

import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest
from test_event_counterevidence import DAY, DENIAL, NOW, STATEMENT, event, head, item
from test_intelligence_migration import isolated_database  # noqa: F401

from app import storage as app_storage
from app.industrial_intelligence import counterevidence as rules
from app.industrial_intelligence import counterevidence_scan as scan
from app.industrial_intelligence import counterevidence_stage as stage
from app.industrial_intelligence import identity, storage
from app.industrial_intelligence.counterevidence_sources import evidence_texts


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/event_counterevidence_matching.json").read_text())["pairs"],
    ids=lambda case: case["id"],
)
def test_curated_pair_regression(case):
    actual = any(
        rules.affirmative_match(case["original"], candidate) for candidate in rules.explicit_denials(case["denial"])
    )
    assert actual == case["expected_match"]


def source_article(c, *, original=DENIAL, generated=NOW, status="completed", quality="full_text"):
    from test_intelligence_catalog_projection import _insert_news_article

    from app.news import EVENT_SUMMARY_PROMPT_VERSION

    title = "News with a denial in its second paragraph"
    digest = hashlib.sha256(f"{title}\n{original}".encode()).hexdigest()
    _insert_news_article(
        c,
        article_id="source-article",
        source_id="opec_press",
        title=title,
        canonical_url="https://www.opec.org/news/123",
        raw_text=original,
        created_at=NOW,
        first_seen_at=NOW,
    )
    c.execute("UPDATE news_articles SET content_hash=? WHERE article_id='source-article'", (digest,))
    c.execute(
        """INSERT INTO event_ai_summaries(article_id,factual_summary,summary_status,
              quality_status,fact_summary_status,input_quality,model,prompt_version,source_hash,generated_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "source-article",
            "另一个已核验的主要事实。",
            status,
            status,
            status,
            quality,
            "test",
            EVENT_SUMMARY_PROMPT_VERSION,
            digest,
            generated,
            generated,
        ),
    )
    c.commit()
    row = item(
        c,
        "news-with-hidden-denial",
        "另一个已核验的主要事实。",
        projection_source_type="news_article",
        projection_source_id="source-article",
        content_sha256=digest,
    )
    return stage._body(row)


@pytest.mark.parametrize("price", ["8550.5", "8.55", "-8.55", "+8.55", "8,550.50", "85.5%"])
def test_exact_decimal_and_signed_numbers_match(price):
    claim = f"2026年9月26日，恒力石化PTA报价为{price}元每吨"
    denial = rules.explicit_denials(f"“{claim}”的报道不实。")[0]
    assert rules.affirmative_match(claim + "。", denial)


@pytest.mark.parametrize(
    ("original", "denied"),
    [
        ("85505", "8550.5"),
        ("855", "8.55"),
        ("8.55", "-8.55"),
        ("-8.55", "+8.55"),
        ("8550", "85.50"),
        ("855", "8,55"),
        ("85.5%", "85.5"),
    ],
)
def test_different_numeric_values_never_match(original, denied):
    template = "2026年9月26日，恒力石化PTA报价为{}元每吨"
    denial = rules.explicit_denials(f"“{template.format(denied)}”的报道不实。")[0]
    assert rules.affirmative_match(template.format(original), denial) is None


@pytest.mark.parametrize(
    "claim",
    [
        "Hengli PTA price reached 8550.5 yuan on 2026-09-26",
        "Hengli PTA plant shut down on September 26, 2026",
        "Hengli PTA plant shut down on May 26, 2026",
        "U.S. diesel prices reached 6.50 dollars on 2026-09-26",
    ],
)
def test_english_dates_decimals_and_abbreviations(claim):
    denial = rules.explicit_denials(f"The claim that {claim} is false.")[0]
    assert rules.affirmative_match(claim + ".", denial)


@pytest.mark.parametrize(
    "denial",
    [
        f"公司否认{STATEMENT}。",
        f"公司否认了{STATEMENT}。",
        "The company denied that Hengli PTA plant shut down on 2026-09-26.",
    ],
)
def test_direct_denial_without_quotation_marks(denial):
    candidates = rules.explicit_denials(denial)
    assert len(candidates) == 1
    original = STATEMENT if denial.startswith("公司") else "Hengli PTA plant shut down on 2026-09-26"
    assert rules.affirmative_match(original, candidates[0])


@pytest.mark.parametrize(
    "original",
    [
        "2026年9月26日，恒力石化PTA装置起火。",
        "恒力石化PTA装置于2026年9月26日起火。",
        "恒力石化PTA装置发生火灾，2026年9月26日。",
    ],
)
def test_explicit_date_position_and_bounded_action_paraphrases(original):
    assert rules.affirmative_match(original, rules.explicit_denials(DENIAL)[0])


@pytest.mark.parametrize(
    "text",
    [
        "白宫否认考虑实施美国柴油出口禁令，2026年9月23日，美国。",
        "The White House on Wednesday denied that the Administration is considering a ban on U.S. diesel exports.",
        "Libya's NOC denied declaring force majeure at the El Sharara field.",
    ],
)
def test_real_language_leads_are_discovered_but_not_invented_pairs(text):
    candidates = rules.denial_candidates(text)
    assert candidates
    assert not rules.explicit_denials(text)
    assert all(not rules.affirmative_match("Unrelated original fact.", d) for d in candidates)


@pytest.mark.parametrize(
    "text",
    [
        "North Carolina regulators rejected a gas power plant project.",
        "An earthquake near False Pass, Alaska.",
        "The White House did not deny that a ban is in the works.",
        f"公司未否认{STATEMENT}。",
        f"公司可能否认{STATEMENT}。",
        f"如果公司否认{STATEMENT}。",
    ],
)
def test_non_denial_or_hypothetical_is_not_a_candidate(text):
    assert rules.denial_candidates(text) == []


def test_cartesian_cursor_visits_all_pairs_and_survives_restart(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(stage, "MAX_EVENTS", 2)
    monkeypatch.setattr(stage, "MAX_ITEMS", 2)
    with closing(app_storage.connect()) as c:
        for i in range(5):
            event(c, item(c, f"cursor-{i}", STATEMENT))
        visited = set()
        for _ in range(9):  # ceil(5/2) * ceil(5/2)
            plan = stage.plan_counterevidence(c, cutoff_at=NOW)
            items, _ = scan.page(
                c,
                kind="item",
                cutoff=NOW,
                since="2026-09-19T00:00:00Z",
                after=plan["cursor_before"]["item_after"],
                limit=2,
            )
            visited.update((e, i["item_id"]) for e in plan["scanned_event_ids"] for i in items)
            state_before = storage.snapshot_high_water(c)
            stage.refresh_counterevidence(c, cutoff_at=NOW, dry_run=True)
            assert storage.snapshot_high_water(c) == state_before
            stage.refresh_counterevidence(c, cutoff_at=NOW)
        assert len(visited) == 25
    with closing(app_storage.connect()) as restarted:
        assert scan.cursor(restarted, NOW) == {"event_after": "", "item_after": ""}


def test_newly_visible_old_article_is_in_window(isolated_database):  # noqa: F811
    with closing(app_storage.connect()) as c:
        r = item(c, "old-created-new-visible", DENIAL, at="2026-09-01T00:00:00Z", visible_at=NOW)
        rows, _ = scan.page(c, kind="item", cutoff=NOW, since="2026-09-19T00:00:00Z", after="", limit=5)
        assert [row["item_id"] for row in rows] == [r["item_id"]]


def test_cursor_write_failure_rolls_back_new_counterclaims(isolated_database, monkeypatch):  # noqa: F811
    with closing(app_storage.connect()) as c:
        target = event(c, item(c, "before-counter", STATEMENT))
        item(c, "new-counter", DENIAL)
        before = storage.snapshot_high_water(c)
        monkeypatch.setattr(scan, "append_cursor", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("cursor fail")))
        with pytest.raises(RuntimeError, match="cursor fail"):
            stage.refresh_counterevidence(c, cutoff_at=NOW)
        assert storage.snapshot_high_water(c) == before
        assert head(c, target)[1]["counterevidence"] == []


def test_old_empty_replay_cannot_regress_scan_cursor(isolated_database):  # noqa: F811
    with closing(app_storage.connect()) as c:
        stage.refresh_counterevidence(c, cutoff_at=DAY + "T12:05:00Z")
        before = storage.snapshot_high_water(c)
        with pytest.raises(ValueError, match="cutoff_regression"):
            stage.refresh_counterevidence(c, cutoff_at=NOW)
        assert storage.snapshot_high_water(c) == before


def test_validated_original_restores_omitted_denial_and_persists_quote(isolated_database, monkeypatch):  # noqa: F811
    monkeypatch.setattr(identity, "utc_now_iso", lambda: DAY + "T12:01:00Z")
    with closing(app_storage.connect()) as c:
        target = event(c, item(c, "original-primary-fact", STATEMENT))
        source = source_article(c)
        assert evidence_texts(c, source, NOW)[0] == ("verified_original", DENIAL)
        plan = stage.refresh_counterevidence(c, cutoff_at=NOW)
        assert plan["verified_original_sources"] == 1
        assert plan["appended_revisions"] == 1
        saved = head(c, target)[1]["counterevidence"][0]
        assert DENIAL.rstrip("。") in saved["text"]
        linked = c.execute(
            """SELECT i.content_sha256 FROM intelligence_event_evidence e
                           JOIN intelligence_item_revisions i ON i.item_revision_id=e.item_revision_id
                           WHERE e.evidence_link_id=?""",
            (saved["evidence_link_ids"][0],),
        ).fetchone()
        assert linked["content_sha256"] == source["content_sha256"]


@pytest.mark.parametrize(
    "mutate",
    [
        "future_summary",
        "rejected",
        "partial",
        "raw_changed",
        "source_hash_changed",
        "item_hash_changed",
        "prompt_changed",
    ],
)
def test_original_fallback_preserves_quality_hash_and_time_gates(isolated_database, mutate):  # noqa: F811
    with closing(app_storage.connect()) as c:
        source = source_article(c)
        if mutate == "future_summary":
            c.execute("UPDATE event_ai_summaries SET generated_at='2026-09-27T00:00:00Z'")
        elif mutate == "rejected":
            c.execute("UPDATE event_ai_summaries SET quality_status='rejected'")
        elif mutate == "partial":
            c.execute("UPDATE event_ai_summaries SET input_quality='partial_text'")
        elif mutate == "raw_changed":
            c.execute("UPDATE news_articles SET raw_text='different bytes'")
        elif mutate == "source_hash_changed":
            c.execute("UPDATE event_ai_summaries SET source_hash=?", ("f" * 64,))
        elif mutate == "item_hash_changed":
            source["content_sha256"] = "f" * 64
        else:
            c.execute("UPDATE event_ai_summaries SET prompt_version='old'")
        assert evidence_texts(c, source, NOW) == [("item_excerpt", source["excerpt"])]


def test_other_article_paragraph_cannot_contradict_unrelated_event_fact(isolated_database, monkeypatch):  # noqa: F811
    from app.industrial_intelligence import counterevidence_stage

    with closing(app_storage.connect()) as c:
        target = event(c, item(c, "original-unrelated-fact", "2026年9月26日，PX进口量增加。"))
        item(c, "denial-in-another-story", DENIAL)
        real = counterevidence_stage.evidence_texts

        def extra_paragraph(connection, body, cutoff):
            texts = real(connection, body, cutoff)
            if body["excerpt"].startswith("2026年9月26日，PX"):
                texts.insert(0, ("verified_original", STATEMENT + "。"))
            return texts

        monkeypatch.setattr(counterevidence_stage, "evidence_texts", extra_paragraph)
        assert stage.refresh_counterevidence(c, cutoff_at=NOW)["appended_revisions"] == 0
        assert head(c, target)[1]["counterevidence"] == []


@pytest.mark.parametrize("proof", ["legacy", "valid", "future", "wrong_hash"])
def test_truncated_body_requires_separate_visible_byte_fingerprint(isolated_database, proof):  # noqa: F811
    with closing(app_storage.connect()) as c:
        original = (DENIAL + " Other market news." * 400)[:5000]
        body = source_article(c, original=original)
        full_hash = "e" * 64
        metadata = {"source_content": {"text_chars": 6000, "status": "full_text"}}
        if proof != "legacy":
            metadata["source_content"].update(
                stored_text_sha256=hashlib.sha256(original.encode()).hexdigest() if proof != "wrong_hash" else "f" * 64,
                stored_text_content_hash=full_hash,
                stored_text_verified_at="2026-09-27T00:00:00Z" if proof == "future" else NOW,
            )
        c.execute("UPDATE news_articles SET content_hash=?,raw=?", (full_hash, json.dumps(metadata)))
        c.execute("UPDATE event_ai_summaries SET source_hash=?", (full_hash,))
        body["content_sha256"] = full_hash
        texts = evidence_texts(c, body, NOW)
        expected = "verified_original" if proof == "valid" else "legacy_truncated_original"
        assert texts[0][0] == expected
        assert rules.denial_candidates(texts[0][1])
        if proof == "valid":
            metadata["source_content"]["text_chars"] = 4000  # cleaned length can be shorter than raw prefix
            metadata["source_content"]["stored_text_truncated"] = True
            c.execute("UPDATE news_articles SET raw=?", (json.dumps(metadata),))
            assert evidence_texts(c, body, NOW)[0] == texts[0]


def test_failing_to_rule_out_is_not_denial():
    assert rules.denial_candidates("Analysts were failing to rule out diesel reaching £3 per litre.") == []


def test_denial_followed_by_even_if_is_still_a_candidate():
    text = "The White House denied an export ban is in the works, even if officials had hinted at it earlier."
    assert rules.denial_candidates(text)
    assert rules.explicit_denials(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "The claim that Hengli PTA plant had no fire on 2026-09-26 is false.",
        "The claim that Hengli PTA plant operated without a shutdown on 2026-09-26 is false.",
        f"公司否认“{STATEMENT}”，但该否认不实。",
    ],
)
def test_negative_original_or_disputed_denial_does_not_auto_link(text):
    assert rules.explicit_denials(text) == []

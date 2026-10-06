import sqlite3
from contextlib import closing

import pytest

from app.prediction_evidence_diagnostic import NEWS_COLUMNS, SUMMARY_COLUMNS, export_article_diagnostic
from app.prediction_evidence_inputs import digest


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "diagnostic.sqlite"
    with closing(sqlite3.connect(p)) as c:
        c.execute("CREATE TABLE news_articles (" + ",".join(x + " TEXT" for x in NEWS_COLUMNS) + ", raw TEXT)")
        c.execute(
            "CREATE TABLE event_ai_summaries (article_id TEXT," + ",".join(x + " TEXT" for x in SUMMARY_COLUMNS) + ")"
        )
        c.execute(
            "INSERT INTO news_articles(article_id,canonical_url,raw_text,raw) VALUES(?,?,?,?)",
            ("news-1", "https://example.test/story", "PTA chemical supply report", "{}"),
        )
        c.commit()
    return p


def test_readonly_snapshot_has_requested_scope_missing_list_and_hash(db):
    before = db.read_bytes()
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as c:
        result = export_article_diagnostic(c, ["news-1", "not-found"])
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM news_articles")
    assert db.read_bytes() == before
    assert result["returned_count"] == 1 and result["missing_article_ids"] == ["not-found"]
    assert not result["historical_body_availability_verified"]
    assert result["content_sha256"] == digest({k: v for k, v in result.items() if k != "content_sha256"})


@pytest.mark.parametrize("kwargs", [{"max_bytes": 10}, {"max_rows": 0}, {"timeout_seconds": 46}])
def test_resource_limits_fail_closed(db, kwargs):
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as c, pytest.raises(ValueError):
        export_article_diagnostic(c, ["news-1"], **kwargs)


def test_ids_cannot_inject_sql_and_unrequested_rows_are_not_read(db):
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as c:
        assert export_article_diagnostic(c, ["x'); DROP TABLE news_articles; --"])["rows"] == []
        assert c.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 1


def test_credential_bearing_url_is_refused(db):
    with closing(sqlite3.connect(db)) as c:
        c.execute("UPDATE news_articles SET canonical_url='https://example.test/a?api_key=secret'")
        c.commit()
    with (
        closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as c,
        pytest.raises(ValueError, match="credential"),
    ):
        export_article_diagnostic(c, ["news-1"])


def test_active_caller_transaction_is_preserved(db):
    with closing(sqlite3.connect(db)) as c:
        c.execute("BEGIN")
        with pytest.raises(ValueError, match="own_read_transaction"):
            export_article_diagnostic(c, ["news-1"])
        assert c.in_transaction

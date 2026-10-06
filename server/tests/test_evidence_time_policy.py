from datetime import UTC, datetime

from app.evidence_time_policy import current_evidence_allowed, is_current_question


def test_current_questions_exclude_old_future_and_undated_market_claims():
    now = datetime(2026, 9, 13, tzinfo=UTC)
    assert is_current_question('今天上游成本压力怎么看？')
    assert current_evidence_allowed('market_observation', '2026-09-11', now=now)
    assert not current_evidence_allowed('market_observation', '2026-07-20', now=now)
    assert not current_evidence_allowed('news_article', '2026-09-14', now=now)
    assert not current_evidence_allowed('news_article', '', now=now)
    assert not current_evidence_allowed('political_case_memory', '2026-09-11', now=now)
    assert current_evidence_allowed('knowledge_node', '', now=now)


def test_historical_and_point_in_time_questions_keep_their_existing_boundaries():
    assert not is_current_question('比较今天与去年同期的价格')
    assert not is_current_question('今天成本如何', '2026-07-20')
    assert current_evidence_allowed('news_article', '12 Sep 2026 10:30:00 +0000', now=datetime(2026,9,13,tzinfo=UTC))


def test_reclustering_does_not_refresh_old_publication():
    from app.rag import _cluster_observed_at
    row = {'article_ids':['eia-old'], 'created_at':'2026-09-13', 'updated_at':'2026-09-13'}
    assert _cluster_observed_at(row, {'eia-old':'2025-08-12'}) == '2025-08-12T00:00:00+00:00'
    assert _cluster_observed_at(row, {}) == ''
